"""Hard filters that run before any AI step: title, location, salary, lanes, exclusions."""
import re

# Cities the Scout can resolve a location to. Anything in NC outside this list is too far
# for a 30-minute commute from Durham and is treated as out of range.
NC_CITIES = [
    "Research Triangle Park", "RTP", "Durham", "Raleigh", "Cary", "Morrisville", "Chapel Hill", "Carrboro",
    "Apex", "Holly Springs", "Fuquay-Varina", "Garner", "Wake Forest", "Knightdale", "Wendell", "Zebulon",
    "Clayton", "Hillsborough", "Mebane", "Burlington", "Graham", "Sanford", "Pittsboro", "Butner",
    "Creedmoor", "Smithfield", "Wilson", "Oxford", "Henderson", "Rolesville", "New Hill",
]


def _squash(text):
    return re.sub(r"[^a-z]", "", text.lower())


def _has_word(text, word):
    return re.search(r"(?<![a-z])" + re.escape(word.lower()) + r"(?![a-z])", text.lower()) is not None


def title_passes(title, include, exclude):
    if any(_has_word(title, w) for w in exclude):
        return False
    return any(_has_word(title, w) for w in include)


_SHIFT = re.compile(r"\b(2nd|3rd|second|third|night|nights|overnight|swing|graveyard|weekend|weekends)\b"
                    r"|\b4x10\b|\b(wed|thu|fri)\w*\s*[-–]\s*(sat|sun)", re.I)


def is_shift_role(title):
    """Shift or weekend work named in the title; such roles are out of scope."""
    return _SHIFT.search(title) is not None


def is_excluded_employer(name, excluded):
    n = _squash(name)
    return any(_squash(e) in n for e in excluded)


def resolve_location(location, title="", description=""):
    """Return (work_mode, city) where city is "City, NC" or None.

    work_mode: remote / hybrid / onsite. A remote posting needs no city.
    """
    loc = location or ""
    text = f"{title} {loc}".lower()
    desc = (description or "").lower()
    if "remote" in text or "virtual" in text or "work from home" in text:
        mode = "remote"
    elif "hybrid" in text or "hybrid" in desc:
        mode = "hybrid"
    else:
        mode = "onsite"

    squashed = _squash(loc)
    in_nc = re.search(r"\bNC\b|North Carolina|[-/_]NC\b|US-NC", loc, re.I) is not None
    for city in NC_CITIES:
        if _squash(city) in squashed and (in_nc or city in ("Research Triangle Park", "RTP")):
            return mode, ("Research Triangle Park, NC" if city == "RTP" else f"{city}, NC")
    return mode, None


def is_us_remote(location):
    """Remote postings tied to another country are out of scope."""
    foreign = re.search(r"\b(canada|uk|united kingdom|germany|india|ireland|mexico|netherlands|belgium|"
                        r"switzerland|france|spain|italy|poland|china|japan|australia|singapore|brazil)\b",
                        location or "", re.I)
    return foreign is None


_AMOUNT = r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d+)?\s?[kK]?)"
_RANGE = re.compile(_AMOUNT + r"\s*(?:-|–|—|to)\s*" + _AMOUNT + r"(?:\s*(?:/|per)\s*(hour|hr|year|yr|annum))?", re.I)


def _to_number(raw):
    raw = raw.replace(",", "").replace(" ", "")
    if raw.lower().endswith("k"):
        return float(raw[:-1]) * 1000
    return float(raw)


def parse_salary(text):
    """Return (min, max) annual USD from the first pay range in the text, or (None, None)."""
    for m in _RANGE.finditer(text or ""):
        low, high = _to_number(m.group(1)), _to_number(m.group(2))
        unit = (m.group(3) or "").lower()
        if unit in ("hour", "hr") or high < 300:
            low, high = low * 2080, high * 2080
        if 20_000 <= low <= high <= 600_000:
            return low, high
    return None, None


def match_lanes(text, lanes):
    """Return lane keys whose keywords appear in the text."""
    return [key for key, lane in lanes.items() if any(_has_word(text, kw) for kw in lane["keywords"])]

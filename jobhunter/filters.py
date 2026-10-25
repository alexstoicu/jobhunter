"""Hard filters that run before any AI step: title, location, salary, lanes, exclusions."""
import re
from datetime import date, datetime, timedelta, timezone

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
    """Whole-word match; a trailing * makes it a prefix match ("radiochem*" matches "radiochemistry")."""
    prefix = word.endswith("*")
    pattern = r"(?<![a-z])" + re.escape(word.rstrip("*").lower()) + ("" if prefix else r"(?![a-z])")
    return re.search(pattern, text.lower()) is not None


def _any(text, words):
    return any(_has_word(text, w) for w in words)


def title_verdict(title, rules):
    """Apply the title rules from config.yaml (scout section). Returns (passes, reason)."""
    if _any(title, rules["title_exclude"]):
        return False, "title: excluded role type or level"
    if _any(title, rules["title_supply"]):
        if _any(title, rules["title_development"]):
            return True, None
        return False, "title: supply/logistics without development"
    # Any "quality" title other than quality control counts as QA.
    is_qa = _any(title, rules["title_qa"]) or (_has_word(title, "quality") and not _has_word(title, "quality control"))
    if is_qa and _any(title, rules["title_senior"]):
        return False, "title: senior QA"
    if is_qa or _any(title, rules["title_lab"]):
        return True, None
    return False, "title: not lab or QA"


def posted_date(posted, seen=None):
    """Normalize a feed's posted value to an ISO date, or None if unknown.

    Workday gives relative text ("Posted 3 Days Ago", "Posted 30+ Days Ago"); others give
    ISO timestamps or epoch milliseconds.
    """
    seen = seen or datetime.now(timezone.utc)
    if posted is None or posted == "":
        return None
    if isinstance(posted, (int, float)) or str(posted).isdigit():
        return datetime.fromtimestamp(int(posted) / 1000, timezone.utc).date().isoformat()
    text = str(posted).lower()
    if "today" in text or "just posted" in text:
        return seen.date().isoformat()
    if "yesterday" in text:
        return (seen - timedelta(days=1)).date().isoformat()
    m = re.search(r"(\d+)\+?\s*days? ago", text)
    if m:
        days = int(m.group(1)) + (1 if "+" in text else 0)  # "30+" means older than 30
        return (seen - timedelta(days=days)).date().isoformat()
    m = re.match(r"\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m else None


def age_days(iso_date):
    if not iso_date:
        return None
    return (datetime.now(timezone.utc).date() - date.fromisoformat(iso_date)).days


_SHIFT = re.compile(r"\b(2nd|3rd|second|third|night|nights|overnight|swing|graveyard|weekend|weekends|rotating)\b"
                    r"|\b4x10\b|\b12[- ]?hours?\b|\b(wed|thu|fri)\w*\s*[-–]\s*(sat|sun)"
                    r"|\b[A-D][- ]shift\b", re.I)


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
    return [key for key, lane in lanes.items() if _any(text, lane["keywords"])]


# Section headings in job descriptions. A heading is a short line, optionally ending in ":".
_DUTIES = (r"(key |primary |main |major |essential |core |job |your |role |position )?"
           r"(responsibilities|duties|accountabilities|essential functions|what you('|’)?ll do|what you will do|"
           r"the role|your role|about the role|the opportunity|job description|position summary|job summary|"
           r"position overview|role overview|in this role,? you will|how you will contribute|a typical day)")
_REQUIREMENTS = (r"(basic |minimum |required |preferred |additional |desired |key )?"
                 r"(qualifications|requirements|education|skills|experience|what you('|’)?ll bring|what you bring|"
                 r"what we('|’)?re looking for|who you are|about you|you have|must have|knowledge)")
_STOP = (r"(benefits|what we offer|why join|why you('|’)?ll love|what makes this|why this role|perks|compensation|pay range|salary|"
         r"equal (employment )?opportunity|eeo|about (us|the company)|who we are|our company|our mission|"
         r"life at|working at|join us|accommodation|disclaimer|additional information|#li)")


def _find_heading(text, words):
    """First line that starts with one of the words and reads like a heading, not a bullet.

    A heading ends with ":" or is short and has no "of/with/in/to" (bullets like
    "Experience with HPLC" or "Knowledge of GMP" are not headings).
    """
    for m in re.finditer(r"(?im)^[ \t]*(?:" + words + r")\b(?P<tail>[^\n]{0,40})$", text):
        line = m.group(0).strip()
        if line.endswith(":") or (len(line) <= 45 and not re.search(r"\b(of|with|in|to|for|and the)\b", m.group("tail"), re.I)):
            return m
    return None


def _section(text, start_words, stop_words):
    start = _find_heading(text, start_words)
    if not start:
        return None
    rest = text[start.end():]
    stop = _find_heading(rest, stop_words)
    return rest[:stop.start() if stop else len(rest)].strip() or None


def extract_sections(description, limit=1800):
    """Return {"duties": ..., "requirements": ...}: what the job involves, without the company story.

    Falls back to the description minus its opening paragraph when no headings are found.
    """
    text = description or ""
    duties = _section(text, _DUTIES, _REQUIREMENTS + "|" + _STOP)
    requirements = _section(text, _REQUIREMENTS, _STOP + "|" + _DUTIES)
    if not duties:
        paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        duties = "\n\n".join(paragraphs[1:] if len(paragraphs) > 1 else paragraphs)
        stop = _find_heading(duties, _STOP)
        duties = duties[:stop.start()] if stop else duties
    clip = lambda s: (s[:limit].rsplit("\n", 1)[0] + " …") if s and len(s) > limit else s
    return {"duties": clip(duties.strip()) if duties else None, "requirements": clip(requirements)}

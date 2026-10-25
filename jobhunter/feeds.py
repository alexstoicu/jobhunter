"""Readers for employers' public job feeds.

Each kind provides:
  search(feed, terms) -> list of postings {external_id, title, location, url, posted, description?}
  detail(feed, posting) -> dict of fields to merge in (description, location, work_mode hint)
`description` may be missing after search; the Scout calls detail() only for postings
whose title already passed the filter, to keep requests low.
"""
import html
import re
import time
import urllib.parse

from jobhunter.http import request_json

PAGE = 20          # Workday page size (its maximum)
MAX_PAGES = 5      # per search term
PAUSE = 0.5        # seconds between requests to one feed


def html_to_text(raw):
    if not raw:
        return ""
    text = re.sub(r"<(br|/p|/li|/div|/h\d)\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


# --- Workday: feed = "host|tenant|site" ---

def _wd_base(feed):
    host, tenant, site = feed.split("|")
    return f"https://{host}/wday/cxs/{tenant}/{site}", f"https://{host}/{site}"


def workday_search(feed, terms):
    api, public = _wd_base(feed)
    found = {}
    for term in terms:
        for page in range(MAX_PAGES):
            data = request_json(f"{api}/jobs", {"limit": PAGE, "offset": page * PAGE, "searchText": term, "appliedFacets": {}})
            postings = data.get("jobPostings", [])
            for p in postings:
                path = p["externalPath"]
                found[path] = {
                    "external_id": path.rsplit("/", 1)[-1],
                    "title": p["title"],
                    "location": f"{p.get('locationsText', '')} {path}",
                    "url": public + path,
                    "posted": p.get("postedOn"),
                    "_path": path,
                }
            time.sleep(PAUSE)
            if len(postings) < PAGE:
                break
    return list(found.values())


def workday_detail(feed, posting):
    api, _ = _wd_base(feed)
    info = request_json(api + posting["_path"])["jobPostingInfo"]
    locations = [info.get("location", "")] + info.get("additionalLocations", [])
    return {
        "description": html_to_text(info.get("jobDescription")),
        "location": " ; ".join(l for l in locations if l) + " " + posting["_path"],
        "work_mode_hint": info.get("remoteType", ""),
        "url": info.get("externalUrl") or posting["url"],
    }


# --- Phenom careers sites: feed = "host|refNum" ---

def phenom_search(feed, terms):
    host, ref = feed.split("|")
    found = {}
    for term in terms:
        payload = {
            "lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results",
            "ddoKey": "refineSearch", "from": 0, "jobs": True, "counts": False, "all_fields": [],
            "size": 100, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
            "pageId": "page20", "siteType": "external", "keywords": term, "global": True,
            "selected_fields": {}, "locationData": {}, "refNum": ref,
        }
        jobs = request_json(f"https://{host}/widgets", payload)["refineSearch"]["data"]["jobs"]
        for j in jobs:
            found[j["jobId"]] = {
                "external_id": j["jobId"],
                "title": j["title"],
                "location": " ; ".join(j.get("multi_location") or [j.get("location", "")]),
                "url": j.get("applyUrl") or f"https://{host}/us/en/job/{j['jobSeqNo']}",
                "posted": j.get("postedDate"),
                "description": j.get("descriptionTeaser", ""),
            }
        time.sleep(PAUSE)
    return list(found.values())


def phenom_detail(feed, posting):
    return {}  # the teaser is all Phenom search returns; the Screener reads the full page later


# --- SmartRecruiters: feed = company slug ---

def smartrecruiters_search(feed, terms):
    found = {}
    for term in terms:
        data = request_json(f"https://api.smartrecruiters.com/v1/companies/{feed}/postings"
                            f"?q={urllib.parse.quote(term)}&country=us&limit=100")
        for p in data.get("content", []):
            loc = p.get("location", {})
            found[p["id"]] = {
                "external_id": p["id"],
                "title": p["name"],
                "location": f"{loc.get('city', '')}, {loc.get('region', '')} {'Remote' if loc.get('remote') else ''}",
                "url": f"https://jobs.smartrecruiters.com/{feed}/{p['id']}",
                "posted": p.get("releasedDate"),
            }
        time.sleep(PAUSE)
    return list(found.values())


def smartrecruiters_detail(feed, posting):
    data = request_json(f"https://api.smartrecruiters.com/v1/companies/{feed}/postings/{posting['external_id']}")
    sections = data.get("jobAd", {}).get("sections", {})
    text = "\n\n".join(html_to_text(s.get("text")) for s in sections.values() if isinstance(s, dict))
    return {"description": text, "url": data.get("postingUrl") or posting["url"]}


# --- Lever: feed = company slug (one call returns everything, with descriptions) ---

def lever_search(feed, terms):
    out = []
    for p in request_json(f"https://api.lever.co/v0/postings/{feed}?mode=json"):
        cats = p.get("categories", {})
        lists = "\n".join(f"{l.get('text', '')}\n{html_to_text(l.get('content'))}" for l in p.get("lists", []))
        out.append({
            "external_id": p["id"],
            "title": p["text"],
            "location": f"{cats.get('location', '')} {p.get('workplaceType', '')}",
            "url": p.get("hostedUrl"),
            "posted": p.get("createdAt"),
            "description": f"{p.get('descriptionPlain', '')}\n{lists}\n{p.get('additionalPlain', '')}",
        })
    return out


def lever_detail(feed, posting):
    return {}


# --- Greenhouse: feed = board slug ---

def greenhouse_search(feed, terms):
    data = request_json(f"https://boards-api.greenhouse.io/v1/boards/{feed}/jobs?content=true")
    return [{
        "external_id": str(j["id"]),
        "title": j["title"],
        "location": j.get("location", {}).get("name", ""),
        "url": j.get("absolute_url"),
        "posted": j.get("updated_at"),
        "description": html_to_text(html.unescape(j.get("content", ""))),
    } for j in data.get("jobs", [])]


def greenhouse_detail(feed, posting):
    return {}


# --- Ashby: feed = job board slug (one call, with descriptions) ---

def ashby_search(feed, terms):
    data = request_json(f"https://api.ashbyhq.com/posting-api/job-board/{feed}?includeCompensation=true")
    return [{
        "external_id": j["id"],
        "title": j["title"],
        "location": f"{j.get('location', '')} {'Remote' if j.get('isRemote') else ''} {j.get('workplaceType') or ''}",
        "url": j.get("jobUrl"),
        "posted": j.get("publishedAt"),
        "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
    } for j in data.get("jobs", [])]


def ashby_detail(feed, posting):
    return {}


KINDS = {
    "ashby": (ashby_search, ashby_detail),
    "workday": (workday_search, workday_detail),
    "phenom": (phenom_search, phenom_detail),
    "smartrecruiters": (smartrecruiters_search, smartrecruiters_detail),
    "lever": (lever_search, lever_detail),
    "greenhouse": (greenhouse_search, greenhouse_detail),
}

"""BLS OEWS wage percentiles for the Durham-Chapel Hill and Raleigh-Cary metro areas.

Free BLS public API (v1, no key: 25 series per request, 25 requests per day). Cached in
data/wages.json and refreshed every 90 days; OEWS data is published once a year.
"""
import json
from datetime import date, timedelta

from jobhunter import DATA
from jobhunter.http import request_json

CACHE = DATA / "wages.json"
AREAS = {"0020500": "Durham-Chapel Hill", "0039580": "Raleigh-Cary"}
OCCUPATIONS = {
    "192031": "Chemists",
    "194031": "Chemical technicians",
    "191042": "Medical scientists",
    "191021": "Biochemists and biophysicists",
    "119121": "Natural sciences managers",
    "131041": "Compliance officers",
    "131081": "Logisticians",
    "194099": "Life, physical, social science technicians (other)",
    # alternative track
    "131082": "Project management specialists",
    "273042": "Technical writers",
    "131111": "Management analysts (consultants)",
    "151211": "Computer systems analysts",
    "151253": "Software QA analysts and testers",
    "113013": "Facilities managers",
    "131199": "Business operations specialists (other)",
}
PERCENTILES = {"11": "p10", "12": "p25", "13": "median", "14": "p75", "15": "p90"}


def _series():
    for area in AREAS:
        for occ in OCCUPATIONS:
            for code in PERCENTILES:
                yield f"OEUM{area}000000{occ}{code}"


def fetch():
    ids = list(_series())
    table = {}
    for i in range(0, len(ids), 25):
        data = request_json("https://api.bls.gov/publicAPI/v1/timeseries/data/", {"seriesid": ids[i:i + 25]})
        for s in data["Results"]["series"]:
            if not s["data"]:
                continue
            sid, latest = s["seriesID"], s["data"][0]
            area, occ, code = sid[4:11], sid[17:23], sid[23:25]
            row = table.setdefault(f"{OCCUPATIONS[occ]} | {AREAS[area]}", {"year": latest["year"]})
            try:
                row[PERCENTILES[code]] = int(float(latest["value"]))
            except ValueError:
                pass  # "-" = suppressed
    return table


def load():
    """Return {"Chemists | Durham-Chapel Hill": {"year": ..., "p10": ..., ...}, ...}."""
    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if date.fromisoformat(cached["fetched"]) > date.today() - timedelta(days=90):
            return cached["table"]
    table = fetch()
    CACHE.write_text(json.dumps({"fetched": date.today().isoformat(), "table": table}, indent=1), encoding="utf-8")
    return table


def as_text(table):
    lines = ["Occupation | Metro area | year: 10th / 25th / median / 75th / 90th percentile annual wage (USD)"]
    for key, r in sorted(table.items()):
        vals = " / ".join(f"{r[p]:,}" if p in r else "n/a" for p in PERCENTILES.values())
        lines.append(f"{key} | {r['year']}: {vals}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(as_text(load()))

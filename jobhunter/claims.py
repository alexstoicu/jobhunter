"""Load and validate the claim bank (private/claims.yaml).

The claim bank is the only source of resume content. The Verifier uses
`claim_ids()` to reject any tailored bullet that cites an unknown claim.
"""
import sys
from datetime import date

import yaml

from jobhunter import PRIVATE

CLAIMS_PATH = PRIVATE / "claims.yaml"
# A break between roles longer than this is reported as a gap.
MAX_GAP_MONTHS = 1


def load(path=CLAIMS_PATH):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _items(bank):
    """Yield every (id, text) pair in the bank."""
    for item in bank["summary"]:
        yield item["id"], item["text"]
    for group in bank["skills"].values():
        for item in group:
            yield item["id"], item["text"]
    for role in bank["roles"]:
        for item in role["bullets"]:
            yield item["id"], item["text"]
    for item in bank["education"]:
        yield item["id"], item["degree"]
    for item in bank["recognition"] + bank["languages"]:
        yield item["id"], item["text"]


def claim_ids(bank):
    return {cid for cid, _ in _items(bank)}


def _month(value):
    """'2024-09' -> (2024, 9); 'present' -> this month."""
    if value == "present":
        today = date.today()
        return today.year, today.month
    year, month = str(value).split("-")
    return int(year), int(month)


def _months_between(a, b):
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


def gaps(bank):
    """Breaks between consecutive roles in the master, as (earlier_id, later_id, months).

    These are facts of the master, not errors. The Verifier uses them to make sure
    a tailored resume never shows a gap the master doesn't have.
    """
    roles = sorted(bank["roles"], key=lambda r: _month(r["start"]))
    found = []
    for earlier, later in zip(roles, roles[1:]):
        months = _months_between(_month(earlier["end"]), _month(later["start"]))
        if months > MAX_GAP_MONTHS:
            found.append((earlier["id"], later["id"], months))
    return found


def validate(bank):
    """Return a list of problems; empty means the bank is valid."""
    problems = []

    # An unquoted comma in a flow-style item ({id: x, text: a, b}) splits the text into stray keys.
    flow_items = [i for g in bank["skills"].values() for i in g] + bank["recognition"] + bank["languages"]
    flow_items += [b for r in bank["roles"] for b in r["bullets"]]
    for item in flow_items:
        stray = set(item) - {"id", "text"}
        if stray:
            problems.append(f"{item.get('id')}: text cut at a comma (quote it): stray keys {sorted(stray)}")

    seen = set()
    for cid, text in _items(bank):
        if cid in seen:
            problems.append(f"duplicate id: {cid}")
        seen.add(cid)
        if not text or not str(text).strip():
            problems.append(f"empty text: {cid}")

    for role in bank["roles"]:
        for field in ("id", "title", "start", "end", "bullets"):
            if not role.get(field):
                problems.append(f"role {role.get('id', '?')}: missing {field}")
        for item in role["bullets"]:
            if not item["id"].startswith(role["id"] + "."):
                problems.append(f"bullet {item['id']} not prefixed with role id {role['id']}")

    return problems


if __name__ == "__main__":
    bank = load()
    problems = validate(bank)
    for p in problems:
        print("PROBLEM:", p)
    for earlier, later, months in gaps(bank):
        print(f"gap in master: {months} months between {earlier} and {later}")
    print(f"{len(claim_ids(bank))} claims, {len(bank['roles'])} roles, {len(problems)} problems")
    sys.exit(1 if problems else 0)

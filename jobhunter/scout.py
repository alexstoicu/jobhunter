"""Scout + Filter: read every watchlist feed, store new postings, apply the hard filters.

Run: .venv\\Scripts\\python -m jobhunter.scout [--only "Employer name"]
"""
import argparse
import sys
import traceback
from datetime import datetime, timedelta, timezone

import yaml

from jobhunter import PRIVATE, ROOT, db, filters, geo
from jobhunter.feeds import KINDS


def load_settings():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    with open(PRIVATE / "profile.yaml", encoding="utf-8") as f:
        profile = yaml.safe_load(f)
    with open(PRIVATE / "watchlist.yaml", encoding="utf-8") as f:
        watchlist = yaml.safe_load(f)["employers"]
    return config, profile, watchlist


def evaluate(conn, job, config, profile):
    """Apply the hard filters to a stored job. Returns (status, reason) and fills derived columns."""
    scout = config["scout"]
    excluded = profile["excluded_employers"] + profile.get("crossed_out_employers", [])
    if filters.is_excluded_employer(job["employer"], excluded):
        return "filtered_out", "excluded employer"

    mode, city = filters.resolve_location(job["location"], job["title"], job["description"])
    # Workday's remoteType, e.g. "Fully Remote", "Hybrid (Remote & On-site)", "On-site". Check hybrid first.
    hint = (job.get("work_mode_hint") or "").lower()
    if "hybrid" in hint:
        mode = "hybrid"
    elif "remote" in hint:
        mode = "remote"
    elif "on-site" in hint or "onsite" in hint:
        mode = "onsite"
    job["work_mode"], job["city"] = mode, city

    if mode == "remote":
        if not filters.is_us_remote(job["location"]):
            return "filtered_out", "remote outside the US"
    else:
        if not city:
            return "filtered_out", "location outside the Triangle"
        commute = geo.drive(conn, profile["home_address"], city)
        if not commute:
            return "filtered_out", f"could not route to {city}"
        job["drive_min"], job["drive_miles"] = commute
        if commute[0] > profile["max_commute_minutes"]:
            return "filtered_out", f"commute {commute[0]:.0f} min"

    job["salary_min"], job["salary_max"] = filters.parse_salary(job["description"])
    if job["salary_max"] and job["salary_max"] < profile["salary_floor_usd"]:
        return "filtered_out", f"pay up to ${job['salary_max']:,.0f}"

    lanes = filters.match_lanes(f"{job['title']}\n{job['description']}", config["lanes"])
    job["lanes"] = ",".join(lanes)
    if not lanes:
        return "filtered_out", "no lane keywords"

    cutoff = (datetime.now(timezone.utc) - timedelta(days=scout["repost_window_days"])).isoformat()
    repost = conn.execute(
        "SELECT id FROM jobs WHERE fingerprint = ? AND external_id != ? AND first_seen >= ? "
        "AND status NOT IN ('filtered_out')", (job["fingerprint"], job["external_id"], cutoff)).fetchone()
    if repost:
        return "duplicate", f"repost of job {repost['id']}"

    return "candidate", None


def fingerprint(employer, title, location):
    norm = lambda s: " ".join(filters._squash(w) for w in (s or "").split())
    return f"{norm(employer)}|{norm(title)}|{norm(location)[:40]}"


def scout_employer(conn, employer, config, profile):
    search, detail = KINDS[employer["kind"]]
    scout = config["scout"]
    postings = search(employer["feed"], scout["search_terms"])
    counts = {"seen": len(postings), "new": 0, "candidate": 0}
    stamp = db.now()

    for p in postings:
        key = (employer["kind"], employer["name"], p["external_id"])
        existing = conn.execute("SELECT id FROM jobs WHERE source = ? AND employer = ? AND external_id = ?", key).fetchone()
        if existing:
            conn.execute("UPDATE jobs SET last_seen = ? WHERE id = ?", (stamp, existing["id"]))
            continue
        counts["new"] += 1

        job = {"employer": employer["name"], "description": p.get("description") or "", **p}
        job["posted"] = filters.posted_date(p.get("posted"))
        age = filters.age_days(job["posted"])
        title_ok, title_reason = filters.title_verdict(p["title"], scout)
        if age is not None and age > scout["max_age_days"]:
            status, reason = "filtered_out", f"posted over {scout['max_age_days']} days ago"
        elif not title_ok:
            status, reason = "filtered_out", title_reason
        elif filters.is_shift_role(p["title"]):
            status, reason = "filtered_out", "shift or weekend work"
        else:
            try:
                job.update(detail(employer["feed"], p))
            except Exception as e:  # keep what search returned; the filters still run
                print(f"  detail failed for {p['title']}: {e}", file=sys.stderr)
            job["fingerprint"] = fingerprint(employer["name"], p["title"], job.get("location"))
            status, reason = evaluate(conn, job, config, profile)

        cur = conn.execute(
            """INSERT INTO jobs (source, employer, external_id, title, location, url, posted, description,
                   salary_min, salary_max, work_mode, city, drive_min, drive_miles, lanes, fingerprint,
                   status, status_reason, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (*key, p["title"], job.get("location"), job.get("url"), job["posted"], job.get("description"),
             job.get("salary_min"), job.get("salary_max"), job.get("work_mode"), job.get("city"),
             job.get("drive_min"), job.get("drive_miles"), job.get("lanes"),
             job.get("fingerprint") or fingerprint(employer["name"], p["title"], p.get("location")),
             status, reason, stamp, stamp))
        db.log_event(conn, cur.lastrowid, "found", f"{status}: {reason or ''}")
        if status == "candidate":
            counts["candidate"] += 1

    # Candidates the employer no longer lists are closed; ones past the age limit expire.
    for row in conn.execute("SELECT id FROM jobs WHERE employer = ? AND status = 'candidate' AND last_seen < ?",
                            (employer["name"], stamp)).fetchall():
        conn.execute("UPDATE jobs SET status = 'closed', status_reason = 'no longer listed' WHERE id = ?", (row["id"],))
        db.log_event(conn, row["id"], "closed")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=scout["max_age_days"])).date().isoformat()
    for row in conn.execute("SELECT id FROM jobs WHERE employer = ? AND status = 'candidate' AND posted < ?",
                            (employer["name"], cutoff)).fetchall():
        conn.execute("UPDATE jobs SET status = 'expired', status_reason = 'older than the age limit' WHERE id = ?", (row["id"],))
        db.log_event(conn, row["id"], "expired")
    conn.commit()
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="scout a single employer by name")
    args = parser.parse_args(argv)

    config, profile, watchlist = load_settings()
    crossed = {n.lower() for n in profile.get("crossed_out_employers", [])}
    conn = db.connect()
    run = conn.execute("INSERT INTO runs (started, step) VALUES (?, 'scout')", (db.now(),)).lastrowid
    totals = {"employers": 0, "seen": 0, "new": 0, "candidate": 0, "errors": 0}

    for employer in watchlist:
        if employer["kind"] not in KINDS or employer["name"].lower() in crossed:
            continue
        if args.only and employer["name"].lower() != args.only.lower():
            continue
        try:
            counts = scout_employer(conn, employer, config, profile)
            print(f"{employer['name']:32} seen {counts['seen']:4}  new {counts['new']:4}  candidates {counts['candidate']:3}")
            totals["employers"] += 1
            for k in ("seen", "new", "candidate"):
                totals[k] += counts[k]
        except Exception:
            totals["errors"] += 1
            print(f"{employer['name']:32} ERROR", file=sys.stderr)
            traceback.print_exc()

    conn.execute("UPDATE runs SET finished = ?, summary = ? WHERE id = ?", (db.now(), yaml.safe_dump(totals), run))
    conn.commit()
    print(totals)


if __name__ == "__main__":
    main()

"""Screener: one Claude call per batch of candidates -> fit score, job card fields, salary estimate.

Then ranks screened jobs and queues the day's top N for preparation (Tailor, Stage 3).

Run: .venv\\Scripts\\python -m jobhunter.screen [--limit N] [--dry-run]
"""
import argparse
import json
import re
import sys
from datetime import date

import yaml

from jobhunter import PRIVATE, claude, db, filters, wages
from jobhunter.scout import load_settings

SCHEMA = {
    "type": "object",
    "required": ["jobs"],
    "properties": {"jobs": {"type": "array", "items": {
        "type": "object",
        "required": ["id", "drop", "drop_reason", "fit_score", "lane", "role_type", "seniority", "manages_people",
                     "summary", "why", "matched", "gaps", "red_flags", "phd_listed", "travel_pct", "salary", "apply_figure"],
        "properties": {
            "id": {"type": "integer"},
            "drop": {"type": "boolean"},
            "drop_reason": {"type": "string"},
            "fit_score": {"type": "integer", "minimum": 0, "maximum": 100},
            "lane": {"enum": ["radiochem", "analytical", "el", "qa", "dev_supply", "other"]},
            "role_type": {"enum": ["lab", "qa", "validation", "management", "development_supply", "other"]},
            "seniority": {"enum": ["entry", "associate", "mid", "senior", "principal", "manager"]},
            "manages_people": {"type": "boolean"},
            "summary": {"type": "string"},
            "why": {"type": "string"},
            "matched": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "red_flags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "phd_listed": {"type": "boolean"},
            "travel_pct": {"type": ["integer", "null"]},
            "salary": {"type": "object", "required": ["source", "min", "max", "basis", "confidence"], "properties": {
                "source": {"enum": ["posted", "estimate"]},
                "min": {"type": "integer"}, "max": {"type": "integer"},
                "basis": {"type": "string"},
                "confidence": {"enum": ["low", "medium", "high"]}}},
            "apply_figure": {"type": "integer"},
        }}}},
}


def profile_text(bank):
    """The candidate profile from the claim bank. Contact details are left out on purpose."""
    lines = ["SUMMARY"] + [f"- {s['text']}" for s in bank["summary"]]
    lines.append("\nSKILLS")
    for group, items in bank["skills"].items():
        lines.append(f"- {group.replace('_', ' ')}: " + ", ".join(i["text"] for i in items))
    lines.append("\nEXPERIENCE")
    for r in bank["roles"]:
        where = ", ".join(x for x in (r.get("employer"), r.get("location")) if x)
        lines.append(f"* {r['title']} | {where} | {r['start']} to {r['end']}")
        if r.get("lanes"):  # relevant roles in full; others by title and dates only
            lines += [f"  - {b['text']}" for b in r["bullets"]]
    lines.append("\nEDUCATION")
    for e in bank["education"]:
        lines.append(f"- {e['degree']}, {e['school']} ({e['start']} to {e['end']}); {e.get('note', '')}")
    return "\n".join(lines)


def system_prompt(bank, profile, wage_table):
    return f"""You screen job postings for one candidate and return JSON only, matching the schema.

CANDIDATE
{profile_text(bank)}

WHAT HE WANTS
- Lab or lab-related work. Lane priority: radiochemistry/radiopharma QC first; analytical chemistry and method
  development/validation and extractables & leachables next; drug product development supply/logistics set-up
  (lab or supply-chain start-up/implementation for a manufacturer, never steady-state operations) after that;
  QA/GxP compliance last and only at associate or mid level.
- Level: he is currently Scientist I / lab manager / SME with 10+ years of industry experience. Individual
  contributor roles from associate to senior/principal scientist and lab manager roles fit. Director level and
  above does not.
- PhD-level roles are welcome: his M.Sc. plus 10+ years of experience matches PhD-level expectations. Never treat
  "PhD required/preferred" as a gap or red flag; just set phd_listed.
- No shift, night or weekend work. Travel up to 50%. No relocation; on-site/hybrid must be in the Triangle (NC)
  or the role fully remote in the US.
- Salary floor: ${profile['salary_floor_usd']:,}/year.

DROP A JOB (drop=true, short drop_reason) WHEN
- it is not lab or lab-related work (e.g. IT, clerical, sales, purely computational, clinical/patient-facing);
- it is mainly cell/molecular biology research with no chemistry, analytical or QC component;
- it requires shift/night/weekend work, more than 50% travel, relocation, a license he lacks
  (pharmacist, MLS/ASCP, PE), or a security clearance;
- it is director level or above, senior/lead QA, or steady-state supply/logistics operations;
- the posted pay range tops out below the salary floor.

FIT SCORE (0-100), for jobs not dropped
90+ near-perfect lane and level, nearly all requirements met · 75-89 strong · 60-74 plausible stretch · below 60 weak.
Judge only against the candidate's real experience above; do not assume skills he does not list.

FIELDS
- summary: 1-2 sentences on what the person in this job actually does day to day.
- why: one line on the fit.
- matched / gaps: up to 5 short items each (12 words max), concrete.
- red_flags: things he should know (heavy travel, on-call, contract/temp, hazardous work, unusual hours,
  required certifications). Never list a PhD requirement here.
- travel_pct: if stated, else null.
- salary: if the posting gives a range, source=posted with that range. Otherwise source=estimate: estimate the
  annual range for this role in the Raleigh-Durham area from the BLS table below, the comparable posted ranges
  given with the job, and the role's level; say which in basis (one line) and set confidence.
- apply_figure: a figure in the upper third of the salary range (min + 0.7 x (max - min)), rounded to the nearest
  1,000, never below the salary floor.

BLS OEWS WAGES
{wages.as_text(wage_table)}"""


def _words(title):
    stop = {"i", "ii", "iii", "iv", "sr", "senior", "associate", "the", "and", "of", "in", "a", "-", "&"}
    return {w for w in re.findall(r"[a-z]+", title.lower()) if w not in stop and len(w) > 1}


def comparables(conn, job, limit=6):
    """Posted pay ranges of the most similar titles the Scout has seen (any status)."""
    words = _words(job["title"])
    rows = conn.execute("""SELECT employer, title, location, salary_min, salary_max FROM jobs
                           WHERE salary_min IS NOT NULL AND id != ?""", (job["id"],)).fetchall()
    scored = sorted(((len(words & _words(r["title"])), r) for r in rows), key=lambda x: -x[0])
    return [f"{r['title']} at {r['employer']} ({(r['location'] or '')[:40]}): ${r['salary_min']:,.0f}-${r['salary_max']:,.0f}"
            for score, r in scored[:limit] if score > 0]


def job_prompt(conn, job):
    sections = filters.extract_sections(job["description"], limit=2500)
    where = "Remote (US)" if job["work_mode"] == "remote" else f"{job['work_mode']} in {job['city']}, {job['drive_min']:.0f} min drive"
    posted_pay = (f"${job['salary_min']:,.0f}-${job['salary_max']:,.0f}" if job["salary_max"] else "not posted")
    parts = [f"### JOB id={job['id']}", f"Title: {job['title']}", f"Employer: {job['employer']}",
             f"Location: {where}", f"Posted pay: {posted_pay}",
             f"Duties:\n{sections['duties'] or '(none found)'}",
             f"Requirements:\n{sections['requirements'] or '(none found)'}"]
    if not job["salary_max"]:
        comps = comparables(conn, job)
        parts.append("Comparable posted ranges:\n" + ("\n".join(f"- {c}" for c in comps) if comps else "- none"))
    return "\n".join(parts)


def rank(job, screen, config):
    s = config["screener"]
    score = screen["fit_score"] + s["lane_bonus"].get(screen["lane"], 0)
    age = filters.age_days(job["posted"])
    if age is not None and age <= 3:
        score += s["fresh_bonus"]
    if job["work_mode"] == "remote" or (job["drive_min"] or 99) <= 15:
        score += s["near_bonus"]
    return score


def queue_today(conn, config):
    """Pick today's applications to prepare: best-ranked screened candidates, up to the daily number."""
    today = date.today().isoformat()
    want = config["pilot"]["prepare_per_day"]
    have = conn.execute("SELECT COUNT(*) FROM jobs WHERE queued_on = ?", (today,)).fetchone()[0]
    picks = conn.execute("""SELECT id FROM jobs WHERE status = 'candidate' AND fit_score >= ? AND queued_on IS NULL
                            ORDER BY rank_score DESC LIMIT ?""",
                         (config["screener"]["min_fit"], max(0, want - have))).fetchall()
    for r in picks:
        conn.execute("UPDATE jobs SET status = 'queued', queued_on = ? WHERE id = ?", (today, r["id"]))
        db.log_event(conn, r["id"], "queued", "picked for preparation")
    conn.commit()
    return len(picks)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="screen at most N jobs this run")
    parser.add_argument("--dry-run", action="store_true", help="print the first prompt and exit")
    args = parser.parse_args(argv)

    config, profile, _ = load_settings()
    s = config["screener"]
    with open(PRIVATE / "claims.yaml", encoding="utf-8") as f:
        bank = yaml.safe_load(f)
    conn = db.connect()
    limit = min(args.limit or s["max_jobs_per_run"], s["max_jobs_per_run"])
    jobs = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status = 'candidate' AND screened_at IS NULL ORDER BY posted DESC LIMIT ?", (limit,))]
    system = system_prompt(bank, profile, wages.load())

    if args.dry_run:
        print(system, "\n\n=== USER ===\n", "\n\n".join(job_prompt(conn, j) for j in jobs[:s["batch_size"]]))
        return

    screened = dropped = failed = 0
    for i in range(0, len(jobs), s["batch_size"]):
        batch = jobs[i:i + s["batch_size"]]
        prompt = "Screen these jobs. Return one entry per job, with the same id.\n\n" + "\n\n".join(job_prompt(conn, j) for j in batch)
        try:
            result = claude.run(conn, "screen", system, prompt, SCHEMA, model=s["model"], jobs=len(batch))
        except Exception as e:
            failed += len(batch)
            print(f"batch failed: {e}", file=sys.stderr)
            continue
        by_id = {r["id"]: r for r in result["jobs"]}
        for job in batch:
            r = by_id.get(job["id"])
            if not r:
                failed += 1
                continue
            r["apply_figure"] = max(r["apply_figure"], profile["salary_floor_usd"])
            status = "screened_out" if r["drop"] else "candidate"
            conn.execute("""UPDATE jobs SET fit_score = ?, screen = ?, screened_at = ?, rank_score = ?, status = ?,
                            status_reason = ? WHERE id = ?""",
                         (r["fit_score"], json.dumps(r), db.now(), rank(job, r, config), status,
                          r["drop_reason"] if r["drop"] else None, job["id"]))
            db.log_event(conn, job["id"], "screened", f"fit {r['fit_score']}" + (f", dropped: {r['drop_reason']}" if r["drop"] else ""))
            screened += 1
            dropped += r["drop"]
        conn.commit()

    queued = queue_today(conn, config)
    print(f"screened {screened}, dropped {dropped}, failed {failed}, queued today {queued}")


if __name__ == "__main__":
    main()

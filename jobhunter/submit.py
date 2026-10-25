"""Submitter: fill the employer's application form for approved jobs.

Status flow:  approved -> ready (filled up to Review; waits for Alex's Submit tap while
auto_submit is off) -> submit_confirmed -> submitted.  Anything the answer bank doesn't cover,
a CAPTCHA, an account problem or an unsupported site -> needs_you, with the details on the
dashboard. Alex's answers are added to the bank and the job goes back to approved.

Run: .venv\\Scripts\\python -m jobhunter.submit [--job ID] [--review-only]
"""
import argparse
import json
import sys
import traceback
from datetime import date
from pathlib import Path

from jobhunter import PRIVATE, db
from jobhunter.scout import load_settings
from jobhunter.workday import Escalation, WorkdayApplication

PROFILE_DIR = PRIVATE / "browser"


def ats_of(url):
    host = (url or "").lower()
    if "myworkdayjobs.com" in host:
        return "workday"
    for name in ("greenhouse", "lever.co", "ashbyhq", "smartrecruiters", "icims", "successfactors", "taleo"):
        if name in host:
            return name.split(".")[0]
    return "other"


def submitted_today(conn):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE substr(submitted_at, 1, 10) = ?",
                        (date.today().isoformat(),)).fetchone()[0]


def save(conn, job, status, record, reason=None):
    fields = {"status": status, "submission": json.dumps(record), "status_reason": reason}
    if status == "submitted":
        fields["submitted_at"] = db.now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job["id"]))
    db.log_event(conn, job["id"], status, reason)
    conn.commit()


def process(conn, job, config, playwright, review_only, force_submit=False):
    cfg = config["submitter"]
    ats = ats_of(job["url"])
    if ats != "workday":
        record = {"needs_you": [{"step": "Start", "problem": f"The Submitter can't fill {ats} forms yet. "
                                 "Apply with the tailored PDF using the link."}], "answers": [], "screens": []}
        save(conn, job, "needs_you", record, f"{ats} not supported yet")
        return "needs_you"

    plan = json.loads(job["resume"])
    pdf = Path(job["resume_dir"]) / f"{plan['file']}.pdf"
    out = Path(job["resume_dir"]) / "submission"
    out.mkdir(exist_ok=True)
    may_submit = not review_only and (force_submit or job["status"] == "submit_confirmed" or cfg["auto_submit"])

    context = playwright.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=not cfg["headed"], viewport={"width": 1280, "height": 900})
    page = context.pages[0] if context.pages else context.new_page()
    app = WorkdayApplication(page, job, conn, pdf, out, submit=may_submit)
    try:
        outcome = app.run()
        status = "submitted" if outcome == "submitted" else "ready"
        save(conn, job, status, app.record)
        return status
    except Escalation as e:
        save(conn, job, "needs_you", app.record, str(e)[:200])
        return "needs_you"
    except Exception as e:
        traceback.print_exc()
        try:
            app.shot("crash")
        except Exception:
            pass
        app.record["needs_you"].append({"step": app.step_name() if page else "", "problem": f"Submitter error: {e}"[:300]})
        save(conn, job, "needs_you", app.record, "submitter error")
        return "needs_you"
    finally:
        context.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=int)
    parser.add_argument("--review-only", action="store_true", help="never click Submit")
    parser.add_argument("--submit", action="store_true",
                        help="with --job: submit when every answer is covered (Alex's explicit go-ahead)")
    args = parser.parse_args(argv)
    if args.submit and not args.job:
        parser.error("--submit needs --job")

    from playwright.sync_api import sync_playwright

    config, _, _ = load_settings()
    conn = db.connect()
    if args.job:
        jobs = [dict(r) for r in conn.execute("SELECT * FROM jobs WHERE id = ?", (args.job,))]
    else:
        jobs = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE status IN ('approved', 'submit_confirmed') ORDER BY rank_score DESC")]
    cap = config["pilot"]["submit_cap_per_day"]
    with sync_playwright() as p:
        for job in jobs:
            if submitted_today(conn) >= cap:
                print(f"daily submission cap ({cap}) reached")
                break
            status = process(conn, job, config, p, args.review_only, force_submit=args.submit)
            print(f"{job['id']} {job['employer']} | {job['title']}: {status}")
    if not jobs:
        print("nothing approved")


if __name__ == "__main__":
    sys.exit(main())

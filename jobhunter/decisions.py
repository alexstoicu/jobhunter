"""Apply Alex's approve/reject decisions from the dashboard to the tracker.

The dashboard writes docs/decisions.enc.json to the repo (encrypted in the browser with the
dashboard password): {"<job id>": {"decision": ..., "reason": ..., "answers": {...}, "at": ...}}.

  prepare           on a candidate     -> queued (Alex's own pick; doesn't use a daily slot)
  approve / reject  on a prepared job  -> approved / rejected
  submit            on a ready job     -> submit_confirmed (the Submitter clicks Submit next run)
  answers           on a needs_you job -> answers are added to the answer bank; job back to approved
Each entry is applied once: only when its "at" is newer than the job's decided_at.

Run: .venv\\Scripts\\python -m jobhunter.decisions
"""
import json
import subprocess
import sys
from datetime import date

import keyring

from jobhunter import ROOT, answers, db
from jobhunter.publish import KEYRING, decrypt

FILE = ROOT / "docs" / "decisions.enc.json"


def main(argv=None):
    pull = subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=ROOT, capture_output=True, text=True)
    if pull.returncode:
        print(f"git pull failed: {pull.stderr.strip()}", file=sys.stderr)
    if not FILE.exists():
        print("no decisions yet")
        return
    decisions = decrypt(json.loads(FILE.read_text(encoding="utf-8")), keyring.get_password(*KEYRING))

    conn = db.connect()
    applied = 0
    for job_id, d in decisions.items():
        row = conn.execute("SELECT status, decided_at FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        at = d.get("at") or db.now()
        if not row or (row["decided_at"] and row["decided_at"] >= at):
            continue  # unknown job or already applied
        status, kind = row["status"], d["decision"]
        if status == "candidate" and kind == "prepare":
            conn.execute("UPDATE jobs SET queued_on = ?, picked_by = 'alex' WHERE id = ?",
                         (date.today().isoformat(), int(job_id)))
            new = "queued"
        elif status == "prepared" and kind in ("approve", "reject"):
            new = "approved" if kind == "approve" else "rejected"
        elif status == "ready" and kind == "submit":
            new = "submit_confirmed"
        elif status == "needs_you" and kind == "answers":
            for question, reply in (d.get("answers") or {}).items():
                if reply:
                    answers.learn(question, reply)
            new = "approved"
        elif kind == "reject" and status in ("approved", "ready", "needs_you"):
            new = "rejected"
        else:
            continue
        conn.execute("UPDATE jobs SET status = ?, status_reason = ?, decided_at = ? WHERE id = ?",
                     (new, d.get("reason"), at, int(job_id)))
        db.log_event(conn, int(job_id), new, d.get("reason") or kind)
        applied += 1
    conn.commit()
    print(f"applied {applied} decisions")


if __name__ == "__main__":
    main()

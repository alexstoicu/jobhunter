"""Apply Alex's approve/reject decisions from the dashboard to the tracker.

The dashboard writes docs/decisions.enc.json to the repo (encrypted in the browser with the
dashboard password): {"<job id>": {"decision": "approve"|"reject", "reason": "...", "at": "..."}}.

Run: .venv\\Scripts\\python -m jobhunter.decisions
"""
import json
import subprocess
import sys

import keyring

from jobhunter import ROOT, db
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
        row = conn.execute("SELECT status FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        if not row or row["status"] != "prepared":
            continue  # already applied, or not awaiting a decision
        status = "approved" if d["decision"] == "approve" else "rejected"
        conn.execute("UPDATE jobs SET status = ?, status_reason = ?, decided_at = ? WHERE id = ?",
                     (status, d.get("reason"), d.get("at") or db.now(), int(job_id)))
        db.log_event(conn, int(job_id), status, d.get("reason"))
        applied += 1
    conn.commit()
    print(f"applied {applied} decisions")


if __name__ == "__main__":
    main()

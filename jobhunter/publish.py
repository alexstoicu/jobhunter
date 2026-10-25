"""Build the dashboard data, encrypt it, and publish it to GitHub Pages (docs/).

The repo is public, so only ciphertext is written: AES-256-GCM with a key derived from the
dashboard password (PBKDF2-SHA256). The page decrypts in the browser. The password lives in
Windows Credential Manager and never touches the repo.

  .venv\\Scripts\\python -m jobhunter.publish --set-password   # once, interactive
  .venv\\Scripts\\python -m jobhunter.publish [--push]
"""
import argparse
import base64
import getpass
import json
import os
import subprocess
import sys

import keyring
import yaml
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from jobhunter import PRIVATE, ROOT, db, filters

DOCS = ROOT / "docs"
KEYRING = ("jobhunter", "dashboard")
ITERATIONS = 600_000


def encrypt(payload, password):
    salt, iv = os.urandom(16), os.urandom(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    key = kdf.derive(password.encode())
    ct = AESGCM(key).encrypt(iv, json.dumps(payload).encode(), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "iter": ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def build_payload(conn):
    rows = lambda sql, *a: [dict(r) for r in conn.execute(sql, a)]
    candidates = rows(
        """SELECT id, employer, title, url, location, city, work_mode, drive_min, drive_miles,
                  salary_min, salary_max, lanes, posted, first_seen, description
           FROM jobs WHERE status = 'candidate' ORDER BY posted DESC, employer""")
    for job in candidates:
        job.update(filters.extract_sections(job.pop("description")))
        job["age_days"] = filters.age_days(job["posted"])
    by_status = rows("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    by_reason = rows("""SELECT status_reason AS reason, COUNT(*) AS n FROM jobs
                        WHERE status = 'filtered_out' GROUP BY status_reason ORDER BY n DESC""")
    by_employer = rows("""SELECT employer, COUNT(*) AS seen, SUM(status = 'candidate') AS candidates
                          FROM jobs GROUP BY employer ORDER BY candidates DESC, seen DESC""")
    last_run = rows("SELECT started, finished, summary FROM runs WHERE step = 'scout' ORDER BY id DESC LIMIT 1")

    with open(PRIVATE / "watchlist.yaml", encoding="utf-8") as f:
        watchlist = [{"name": e["name"], "kind": e["kind"], "note": e.get("note", "")}
                     for e in yaml.safe_load(f)["employers"]]

    return {
        "generated": db.now(),
        "candidates": candidates,
        "stats": {"by_status": by_status, "by_reason": by_reason, "by_employer": by_employer,
                  "last_run": last_run[0] if last_run else None},
        "watchlist": watchlist,
    }


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--set-password", action="store_true")
    parser.add_argument("--push", action="store_true", help="commit and push docs/ to GitHub")
    args = parser.parse_args(argv)

    if args.set_password:
        first = getpass.getpass("Dashboard password: ")
        if len(first) < 12 or first != getpass.getpass("Repeat: "):
            sys.exit("Passwords differ or are shorter than 12 characters.")
        keyring.set_password(*KEYRING, first)
        print("Saved to Windows Credential Manager.")
        return

    password = keyring.get_password(*KEYRING)
    if not password:
        sys.exit("No dashboard password yet. Run: .venv\\Scripts\\python -m jobhunter.publish --set-password")

    payload = build_payload(db.connect())
    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.enc.json").write_text(json.dumps(encrypt(payload, password)), encoding="utf-8")
    print(f"Encrypted {len(payload['candidates'])} candidates -> docs/data.enc.json")

    if args.push:
        git("add", "docs")
        if git("status", "--porcelain", "docs").strip():
            git("commit", "-m", "Update dashboard data")
            git("push")
            print("Pushed.")
        else:
            print("No changes to push.")


if __name__ == "__main__":
    main()

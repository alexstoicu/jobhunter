"""Nightly run (Windows Task Scheduler, 02:00): decisions -> Scout -> Screener -> Tailor -> publish.

Each step runs even if an earlier one failed, so the dashboard always gets refreshed.
Log: data/logs/nightly-YYYY-MM-DD.log
"""
import contextlib
import sys
import traceback
from datetime import date

from jobhunter import DATA

STEPS = [
    ("decisions", "jobhunter.decisions", []),
    ("scout", "jobhunter.scout", []),
    ("screen", "jobhunter.screen", []),
    ("tailor", "jobhunter.tailor", []),
    ("submit", "jobhunter.submit", []),
    ("publish", "jobhunter.publish", ["--push"]),
]
# 12:00 and 18:00: act on Alex's decisions without scouting again.
SUBMIT_STEPS = [s for s in STEPS if s[0] in ("decisions", "submit", "publish")]


def main():
    steps = SUBMIT_STEPS if "--submit-only" in sys.argv else STEPS
    logs = DATA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    failed = []
    with open(logs / f"nightly-{date.today().isoformat()}.log", "a", encoding="utf-8", buffering=1) as log, \
            contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
        for name, module, args in steps:
            print(f"=== {name}")
            try:
                mod = __import__(module, fromlist=["main"])
                mod.main(args)
            except SystemExit as e:
                if e.code not in (0, None):
                    failed.append(name)
                    print(f"{name} exited: {e.code}")
            except ModuleNotFoundError as e:
                print(f"{name} skipped: {e}")  # step not built yet
            except Exception:
                failed.append(name)
                traceback.print_exc()
        print("=== done", "FAILED: " + ", ".join(failed) if failed else "ok")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

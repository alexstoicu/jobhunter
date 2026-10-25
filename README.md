# jobhunter

AI powered job application pipeline with an approve-before-submit dashboard.

Runs on a Windows PC: finds postings, filters and ranks them, prepares tailored applications with Claude Code (headless), and publishes an encrypted review dashboard to GitHub Pages. Nothing is submitted without the owner's approval.

## Layout

| Path | What |
| --- | --- |
| `jobhunter/` | Pipeline code |
| `config.yaml` | Public settings: lanes, pilot volume, schedule |
| `private/` | Claim bank, answer bank, profile (git-ignored, never published) |
| `data/` | Tracker database and logs (git-ignored) |

## Setup

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m jobhunter.claims   # validate the claim bank
```

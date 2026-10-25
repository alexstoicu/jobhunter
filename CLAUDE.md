# JobHunter

Pipeline that finds, ranks and prepares job applications for Alex; he approves each one on a dashboard before anything is submitted. The spec (local, git-ignored) is `Job Application Agent Team — Spec v2.md`.

## Hard rules

- **This repo is public.** Never commit anything from `private/` or `data/`, any resume or cover letter, the home address, phone number, or job-search details. Dashboard data is published only encrypted.
- **Resume content comes only from `private/claims.yaml`.** Select, reorder and reword; never add a claim. Every tailored bullet cites claim IDs.
- **Form answers come only from `private/answer_bank.yaml`.** Unknown question → escalate to Alex, never guess.
- **Nothing is submitted without Alex's approval.** Never accept, decline or negotiate offers, reply to recruiters, or schedule interviews.
- **Never apply to Pace Life Sciences / Pace Analytical or affiliates.**
- **Budget:** Claude Pro only. Use plain code wherever no judgment is needed; batch AI calls; log usage of every `claude -p` call.

## Commands

- Python: `.venv\Scripts\python`
- Validate the claim bank: `.venv\Scripts\python -m jobhunter.claims`

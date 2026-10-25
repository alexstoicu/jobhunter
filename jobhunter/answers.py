"""Answer application-form questions strictly from the answer bank.

Claude maps each question (label, type, options) to an answer the bank supports, or null.
A null on a required question means the Submitter stops and the question goes to Alex.
"""
import json
from datetime import date

import yaml

from jobhunter import PRIVATE, claude

BANK_PATH = PRIVATE / "answer_bank.yaml"

SCHEMA = {
    "type": "object", "required": ["answers"],
    "properties": {"answers": {"type": "array", "items": {
        "type": "object", "required": ["key", "answer", "source"],
        "properties": {
            "key": {"type": "string"},
            "answer": {"type": ["string", "null"]},
            "source": {"type": "string"},
        }}}},
}

SYSTEM = """You fill in a job application form for one candidate, using ONLY the facts below. Return JSON only.

For each question return the answer to enter, or null when the facts don't settle it.
- For dropdown, radio and multi-select questions, the answer must be one of the listed options,
  copied exactly. Pick the option that matches the fact (e.g. fact "White" -> option
  "White (Not Hispanic or Latino)"). If no option matches the fact, answer null.
- For yes/no questions answer with the option text ("Yes"/"No").
- For checkboxes answer "checked" or "unchecked".
- "Have you worked for <company> before?": answer from prior_employment; if the company is not listed
  there, the answer is No.
- Leave optional fields that the facts don't cover null (they stay blank). Never invent website or
  LinkedIn URLs; the candidate does not give a LinkedIn profile.
- Desired salary / salary expectation: use DESIRED SALARY below.
- Never answer legal attestations, signatures or consent to terms unless a fact in "learned" covers
  that exact kind of statement.
- Dates: answer as YYYY-MM-DD. Today is {today}.
- source: the fact key you used (e.g. "work_authorization.requires_sponsorship_now"), or "none".

FACTS (answer bank)
{bank}

JOB CONTEXT
{job}"""


def load_bank():
    with open(BANK_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def answer(conn, questions, job, desired_salary, model="sonnet"):
    """questions: [{key, label, kind, options, required}] -> {key: (answer or None, source)}."""
    if not questions:
        return {}
    bank = load_bank()
    bank.pop("todo", None)
    job_ctx = f"{job['title']} at {job['employer']}. DESIRED SALARY: {desired_salary} (annual USD)."
    system = (SYSTEM.replace("{bank}", yaml.safe_dump(bank, sort_keys=False, allow_unicode=True))
              .replace("{job}", job_ctx).replace("{today}", date.today().isoformat()))
    prompt = "QUESTIONS\n" + json.dumps(questions, indent=1, ensure_ascii=False)
    result = claude.run(conn, "prefill", system, prompt, SCHEMA, model=model, jobs=1)
    return {a["key"]: (a["answer"], a["source"]) for a in result["answers"]}


def learn(question, reply):
    """Append Alex's answer to a new question to the bank, so it isn't asked again."""
    bank_text = BANK_PATH.read_text(encoding="utf-8")
    entry = yaml.safe_dump([{"question": question, "answer": reply, "status": "confirmed"}],
                           allow_unicode=True, sort_keys=False, width=1000)
    indented = "".join("  " + line for line in entry.splitlines(keepends=True))
    if "learned: []" in bank_text:
        bank_text = bank_text.replace("learned: []", "learned:\n" + indented.rstrip("\n"))
    else:
        bank_text = bank_text.rstrip("\n") + "\n" + indented
    BANK_PATH.write_text(bank_text, encoding="utf-8")

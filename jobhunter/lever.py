"""Lever application filler (Playwright).

Lever forms are one page with no account: contact fields, the employer's own questions and an EEO
survey. The page is read, every field is answered from the answer bank (answers.answer) or the
claim bank, and the form is filled. It stops with the form filled (status ready) until Alex taps
Submit on the dashboard; then it fills again (Lever keeps no drafts) and clicks Submit. A CAPTCHA
challenge is never solved: the job goes back to Alex.
"""
import json
import re

from jobhunter import answers
from jobhunter.workday import Escalation, _get

# Runs in the page: one entry per question (inputs sharing a name are one question).
EXTRACT_JS = r"""
() => {
  const groups = new Map();
  for (const e of document.querySelectorAll('form input, form textarea, form select')) {
    if (!e.name || e.type === 'hidden' || e.type === 'submit') continue;
    const box = e.closest('li.application-question') || (e.closest('.application-field') || {}).parentElement
      || e.closest('label') || e.parentElement;
    const labelEl = box.querySelector('.application-label');
    const firstLine = ((box.innerText || '').split('\n').map(t => t.trim()).filter(Boolean)[0]) || '';
    const label = (labelEl ? labelEl.innerText.trim().split('\n')[0] : firstLine).replace(/\s+/g, ' ').slice(0, 300);
    const g = groups.get(e.name) || { name: e.name, label, kinds: new Set(), options: [], required: false, value: '' };
    g.kinds.add(e.type === 'select-one' ? 'select' : e.tagName === 'TEXTAREA' ? 'textarea' : e.type);
    if (e.required || /✱|\*/.test(label)) g.required = true;
    if (e.type === 'radio' || e.type === 'checkbox') {
      const opt = (e.closest('label') || e.parentElement).innerText.trim().replace(/\s+/g, ' ');
      g.options.push(opt);
    } else if (e.tagName === 'SELECT') {
      g.options = Array.from(e.options).map(o => o.text.trim()).filter(t => t && !/^select/i.test(t));
    }
    if (!['radio', 'checkbox', 'file'].includes(e.type)) g.value = e.value || '';
    groups.set(e.name, g);
  }
  return Array.from(groups.values()).map(g => ({ ...g, kinds: Array.from(g.kinds) }));
}
"""

# Left alone on purpose: pronouns, links (LinkedIn is never given), the free-text location widget, marketing consent.
SKIP = re.compile(r"^(pronouns|urls\[|consent\[marketing|location$|selectedLocation)", re.I)


class LeverApplication:
    def __init__(self, page, job, conn, pdf_path, out_dir, submit=False, log=print):
        self.page, self.job, self.conn = page, job, conn
        self.pdf_path, self.out_dir, self.submit, self.log = pdf_path, out_dir, submit, log
        self.bank = answers.load_bank()
        screen = json.loads(job["screen"] or "{}")
        self.salary = screen.get("apply_figure")
        self.record = {"steps": [], "answers": [], "needs_you": [], "skipped": [], "screens": [], "account": None}

    def shot(self, name):
        path = self.out_dir / f"{len(self.record['screens']) + 1:02d}_{name}.jpg"
        self.page.screenshot(path=str(path), full_page=True, type="jpeg", quality=55)
        self.record["screens"].append(str(path))

    def step_name(self):
        return "Application"

    def field(self, name):
        return self.page.locator(f'[name="{name}"]')

    def run(self):
        page = self.page
        url = re.sub(r"/apply/?$", "", self.job["url"].split("?")[0]) + "/apply"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("form", timeout=30000)
        page.wait_for_timeout(2500)
        if page.locator("input[type='file'][name='resume']").count():
            page.locator("input[type='file'][name='resume']").first.set_input_files(str(self.pdf_path))
            page.wait_for_timeout(5000)  # Lever reads the resume and fills some fields itself
            self.record["answers"].append({"step": "Application", "question": "Resume", "answer": self.pdf_path.name,
                                           "source": "tailored resume"})
        self.fill_form()
        self.shot("filled")
        if not self.submit:
            self.record["steps"].append("form filled; waiting for Submit")
            return "ready"
        return self.send()

    def known(self, f):
        """Contact fields answered straight from the answer bank."""
        c = {"name": f"{_get(self.bank, 'contact.first_name')} {_get(self.bank, 'contact.last_name')}",
             "email": _get(self.bank, "contact.email"), "phone": _get(self.bank, "contact.phone"),
             "org": "Pace Life Sciences"}
        return c.get(f["name"])

    def fill_form(self):
        fields = [f for f in self.page.evaluate(EXTRACT_JS) if f["kinds"] != ["file"] and not SKIP.match(f["name"])]
        todo = []
        for f in fields:
            direct = self.known(f)
            if direct:
                self.fill(f, direct)
                self.record["answers"].append({"step": "Application", "question": f["label"][:80], "answer": direct,
                                               "source": "contact"})
            elif f["value"] and "radio" not in f["kinds"] and "checkbox" not in f["kinds"]:
                continue  # Lever filled it from the resume
            else:
                todo.append(f)
        if not todo:
            return
        kind_of = lambda f: ("radio" if "radio" in f["kinds"] else "checkboxes" if "checkbox" in f["kinds"] else
                             "select" if "select" in f["kinds"] else "textarea" if "textarea" in f["kinds"] else "text")
        questions = [{"key": f["name"], "label": f["label"], "kind": kind_of(f), "options": f["options"],
                      "required": f["required"]} for f in todo]
        mapped = answers.answer(self.conn, questions, self.job, self.salary)
        for f in todo:
            reply, source = mapped.get(f["name"], (None, "none"))
            if reply is None:
                entry = {"step": "Application", "question": re.sub(r"\s*[✱*]\s*$", "", f["label"]),
                         "kind": kind_of(f), "options": f["options"]}
                self.record["needs_you" if f["required"] else "skipped"].append(entry)
                continue
            try:
                self.fill(f, reply)
                self.record["answers"].append({"step": "Application", "question": f["label"][:80], "answer": reply,
                                               "source": source})
            except Exception as e:
                self.record["needs_you"].append({"step": "Application", "question": f["label"][:80],
                                                 "problem": f"could not fill: {str(e).splitlines()[0][:120]}"})
        if self.record["needs_you"]:
            self.shot("needs_you")
            raise Escalation(f"{len(self.record['needs_you'])} question(s) need Alex")

    def fill(self, f, reply):
        loc = self.field(f["name"])
        kinds = f["kinds"]
        if "radio" in kinds or "checkbox" in kinds:
            wanted = [reply] if "radio" in kinds else [p.strip() for p in reply.split(";")]
            if "checkbox" in kinds and len(f["options"]) == 1:  # a lone consent box
                if reply.lower().startswith(("checked", "yes", "i agree", "true")):
                    loc.first.check(force=True)
                return
            for part in wanted:
                idx = next((i for i, o in enumerate(f["options"]) if o.strip().lower() == part.lower()), None)
                if idx is None:
                    raise ValueError(f"option {part!r} not offered")
                try:
                    loc.nth(idx).check(force=True)
                except Exception:  # custom-styled boxes: click the visible label instead
                    loc.nth(idx).locator("xpath=ancestor::label[1]").click()
        elif "select" in kinds:
            loc.first.select_option(label=reply)
        else:
            loc.first.fill(str(reply))

    def send(self):
        page = self.page
        page.locator("#btn-submit").first.click()
        for _ in range(40):
            page.wait_for_timeout(1000)
            body = page.inner_text("body").lower()
            if "application submitted" in body or "thanks for applying" in body or "/thanks" in page.url:
                self.shot("Submitted")
                self.record["steps"].append("submitted")
                return "submitted"
            if page.locator("iframe[src*='hcaptcha.com'][src*='challenge'], iframe[title*='hCaptcha challenge']").first.is_visible() \
                    if page.locator("iframe[src*='hcaptcha.com']").count() else False:
                self.shot("captcha")
                self.record["needs_you"].append({"step": "Submit", "problem":
                    "The employer's site showed a CAPTCHA, which only you can solve. Open the job page and submit "
                    "with the tailored PDF, then tap \"I applied myself\"."})
                raise Escalation("captcha")
        self.shot("no_confirmation")
        errs = [t.strip() for t in page.locator("[class*='error'], [role='alert']").all_inner_texts() if t.strip()]
        self.record["needs_you"].append({"step": "Submit", "problem": "Clicked Submit but did not see a confirmation.",
                                         "details": errs[:5]})
        raise Escalation("no submission confirmation")

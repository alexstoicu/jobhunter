"""Workday application filler (Playwright).

Walks the standard flow (Create Account/Sign In -> My Information -> My Experience ->
Application Questions -> Voluntary Disclosures -> [Self Identify] -> Review). On every page it
reads the visible form fields, answers them from the answer bank (answers.answer), fills them,
and moves on. It stops at Review unless submit=True and nothing needed Alex. Everything it
did is recorded in `self.record` for the dashboard.
"""
import json
import re
from datetime import date
from urllib.parse import urlparse

from jobhunter import answers, vault

NEXT_NAME = re.compile(r"^\s*(Save and Continue|Next|Continue|Submit)\s*$", re.I)

# Fields answered by code, by data-automation-id fragment -> answer bank path.
KNOWN = {
    "legalNameSection_firstName": "contact.first_name",
    "legalNameSection_lastName": "contact.last_name",
    "addressSection_addressLine1": "contact.address_line_1",
    "addressSection_city": "contact.city",
    "addressSection_postalCode": "contact.postal_code",
    "phone-number": "contact.phone_digits",
    "email": "contact.email",
}

# Runs in the page: describe every visible form field and tag it with data-jh-key.
EXTRACT_JS = r"""
() => {
  const out = [];
  const visible = (e) => !!(e.offsetParent || e.getClientRects().length);
  const labelOf = (box) => {
    const l = box.querySelector('legend, label, [data-automation-id="formLabel"], [data-automation-id="richText"]');
    return (l ? l.innerText : box.innerText).trim().replace(/\s+/g, ' ').slice(0, 300);
  };
  let n = 0;
  for (const box of document.querySelectorAll('[data-automation-id^="formField"]')) {
    if (!visible(box) || box.querySelector('[data-automation-id^="formField"]')) continue;
    const key = 'f' + (n++);
    box.setAttribute('data-jh-key', key);
    const label = labelOf(box);
    const required = /\*/.test(label) || !!box.querySelector('[aria-required="true"], [required]');
    const id = box.getAttribute('data-automation-id') || '';
    const inner = Array.from(box.querySelectorAll('[data-automation-id]')).map(e => e.getAttribute('data-automation-id'));
    let kind = null, options = [], value = '';
    const radios = box.querySelectorAll('input[type="radio"]');
    const checks = box.querySelectorAll('input[type="checkbox"]');
    const listbox = box.querySelector('button[aria-haspopup="listbox"]');
    const multi = box.querySelector('[data-automation-id="multiselectInputContainer"], [data-uxi-widget-type="selectinput"]');
    const dateIn = box.querySelector('[data-automation-id="dateSectionMonth-input"], [data-automation-id="dateSectionYear-input"]');
    const file = box.querySelector('input[type="file"]');
    const text = box.querySelector('input[type="text"], input[type="email"], input[type="tel"], input[type="number"], input:not([type]), textarea');
    if (file) kind = 'file';
    else if (radios.length) {
      kind = 'radio';
      options = Array.from(radios).map(r => (box.querySelector(`label[for="${r.id}"]`) || r.parentElement).innerText.trim());
      const sel = Array.from(radios).find(r => r.checked); value = sel ? sel.value : '';
    } else if (listbox) { kind = 'select'; value = listbox.innerText.trim(); }
    else if (multi) { kind = 'multiselect'; value = Array.from(box.querySelectorAll('[data-automation-id="selectedItem"]')).map(e => e.innerText.trim()).join('; '); }
    else if (dateIn) kind = 'date';
    else if (checks.length === 1) { kind = 'checkbox'; value = checks[0].checked ? 'checked' : 'unchecked'; }
    else if (checks.length > 1) {
      kind = 'checkboxes';
      options = Array.from(checks).map(c => (box.querySelector(`label[for="${c.id}"]`) || c.parentElement).innerText.trim());
    } else if (text) { kind = text.tagName === 'TEXTAREA' ? 'textarea' : 'text'; value = text.value; }
    if (kind) out.push({ key, id, inner, label, required, kind, options, value });
  }
  return out;
}
"""


def _get(bank, path):
    node = bank
    for part in path.split("."):
        node = node.get(part) if isinstance(node, dict) else None
        if node is None:
            return None
    return node.get("answer") if isinstance(node, dict) else node


class Escalation(Exception):
    """Stop: something needs Alex."""


class WorkdayApplication:
    def __init__(self, page, job, conn, pdf_path, out_dir, submit=False, log=print):
        self.page, self.job, self.conn = page, job, conn
        self.pdf_path, self.out_dir, self.submit, self.log = pdf_path, out_dir, submit, log
        self.host = urlparse(job["url"]).netloc
        self.bank = answers.load_bank()
        digits = re.sub(r"\D", "", _get(self.bank, "contact.phone") or "")
        self.bank["contact"]["phone_digits"] = {"answer": digits[-10:]}
        screen = json.loads(job["screen"] or "{}")
        self.salary = screen.get("apply_figure")
        self.record = {"steps": [], "answers": [], "needs_you": [], "screens": [], "account": None}

    # --- helpers ---------------------------------------------------------------------

    def shot(self, name):
        path = self.out_dir / f"{len(self.record['screens']) + 1:02d}_{name}.jpg"
        self.page.screenshot(path=str(path), full_page=True, type="jpeg", quality=55)
        self.record["screens"].append(str(path))

    def click(self, selector_or_locator, timeout=15000):
        loc = self.page.locator(selector_or_locator) if isinstance(selector_or_locator, str) else selector_or_locator
        loc.first.wait_for(state="visible", timeout=timeout)
        try:
            loc.first.click(timeout=5000)
        except Exception:
            loc.first.click(force=True)  # Workday overlays a click_filter div on many buttons

    def settle(self, ms=2000):
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(ms)

    def wait_for_form(self, timeout=45):
        """Wait until the step's fields have rendered and the loading spinner is gone."""
        for _ in range(timeout * 2):
            ready = self.page.evaluate("""() => {
                const vis = (e) => !!(e.offsetParent || e.getClientRects().length);
                const busy = Array.from(document.querySelectorAll('[data-automation-id="loadingSpinner"], [data-automation-id*="Loading"], [aria-busy="true"]')).some(vis);
                const fields = Array.from(document.querySelectorAll('[data-automation-id^="formField"], input[type="file"]')).some(vis);
                const review = /review/i.test((document.querySelector('[data-automation-id="progressBarActiveStep"]') || {}).innerText || '');
                return !busy && (fields || review);
            }""")
            if ready:
                self.page.wait_for_timeout(800)
                return
            self.page.wait_for_timeout(500)
        raise Escalation(f"the {self.step_name() or 'form'} page didn't finish loading")

    def next_button(self):
        return self.page.get_by_role("button", name=NEXT_NAME).last

    def step_name(self):
        loc = self.page.locator("[data-automation-id='progressBarActiveStep']")
        return re.sub(r"^current step \d+ of \d+\s*", "", loc.first.inner_text().strip()) if loc.count() else ""

    def errors(self):
        loc = self.page.locator("[data-automation-id='errorMessage'], [data-automation-id='inputErrorMessage'], "
                                "[data-automation-id='errorBanner'] li")
        return [t.strip() for t in loc.all_inner_texts() if t.strip()]

    # --- flow ------------------------------------------------------------------------

    def run(self):
        page = self.page
        page.goto(self.job["url"], wait_until="networkidle")
        accept = page.locator("[data-automation-id='legalNoticeAcceptButton']")
        if accept.count():
            accept.first.click()
        self.click("[data-automation-id='adventureButton']")
        self.settle(1500)
        manual = page.locator("[data-automation-id='applyManually']")
        if manual.count() and manual.first.is_visible():
            manual.first.click()  # absent when an application is already in progress
            self.settle()
        self.sign_in()

        for _ in range(12):  # pages in the flow, with headroom
            self.wait_for_form()
            step = self.step_name()
            self.log(f"step: {step}")
            if step.lower().startswith("review"):
                return self.review()
            self.fill_page(step)
            self.shot(re.sub(r"\W+", "_", step) or "page")
            self.click(self.next_button())
            self.settle()
            errs = self.errors()
            if errs:
                self.record["needs_you"].append({"step": step, "problem": "Form errors after Save and Continue",
                                                 "details": errs})
                self.shot("errors")
                raise Escalation(f"form errors on {step}: {errs[:3]}")
        raise Escalation("did not reach the Review page")

    def sign_in(self):
        page = self.page
        email = _get(self.bank, "contact.email")
        password, created = vault.get(self.host)
        self.settle(1500)
        email_box = page.locator("[data-automation-id='email']")
        signin_link = page.locator("[data-automation-id='signInLink'], [data-automation-id='utilityButtonSignIn']")
        if not (email_box.count() and email_box.first.is_visible()) and not page.locator(
                "[data-automation-id='createAccountLink']").count():
            if page.locator("[data-automation-id='progressBarActiveStep']").count():
                self.record["account"] = "already signed in"
                return
            if signin_link.count():
                signin_link.first.click()
                self.settle(1000)
        if created:
            create = page.locator("[data-automation-id='createAccountLink']")
            if create.count():
                create.first.click()
                self.settle(1000)
            page.locator("[data-automation-id='email']").first.fill(email)
            page.locator("[data-automation-id='password']").first.fill(password)
            page.locator("[data-automation-id='verifyPassword']").first.fill(password)
            box = page.locator("[data-automation-id='createAccountCheckbox']")
            if box.count():
                box.first.check(force=True)
            self.click("[data-automation-id='createAccountSubmitButton']")
            self.record["account"] = "created"
        else:
            link = page.locator("[data-automation-id='signInLink']")
            if link.count() and not page.locator("[data-automation-id='signInSubmitButton']").count():
                link.first.click()
                self.settle(1000)
            page.locator("[data-automation-id='email']").first.fill(email)
            page.locator("[data-automation-id='password']").first.fill(password)
            self.click("[data-automation-id='signInSubmitButton']")
            self.record["account"] = "signed in"
        self.settle(3000)
        errs = self.errors()
        body = page.inner_text("body").lower()
        if errs or "verify your" in body or "verification" in body:
            self.shot("account")
            self.record["needs_you"].append({"step": "Account", "problem":
                "The site needs something before signing in (email verification or an account problem).",
                "details": errs or ["Check your inbox for a verification email from this employer."]})
            raise Escalation("account step needs attention")

    def fill_page(self, step):
        page = self.page
        if "experience" in step.lower():
            self.upload_resume()
        fields = page.evaluate(EXTRACT_JS)
        todo = []
        for f in fields:
            if f["kind"] == "file":
                continue
            known = next((path for frag, path in KNOWN.items() if frag in f["id"] or frag in f["inner"]), None)
            if known and f["kind"] == "text":
                self.set_text(f, _get(self.bank, known), source=known)
            elif f["value"] and f["kind"] not in ("checkbox",) and f["value"].lower() not in ("select one", ""):
                continue  # already filled (e.g. by Workday from the account)
            else:
                if f["kind"] == "select":
                    f["options"] = self.select_options(f)
                todo.append(f)
        if not todo:
            return
        questions = [{k: f[k] for k in ("key", "label", "kind", "options", "required")} for f in todo]
        mapped = answers.answer(self.conn, questions, self.job, self.salary)
        for f in todo:
            reply, source = mapped.get(f["key"], (None, "none"))
            if reply is None:
                if f["required"]:
                    self.record["needs_you"].append({"step": step, "question": f["label"],
                                                     "kind": f["kind"], "options": f["options"]})
                continue
            try:
                self.fill(f, reply)
                self.record["answers"].append({"step": step, "question": f["label"], "answer": reply, "source": source})
            except Exception as e:
                self.record["needs_you"].append({"step": step, "question": f["label"], "problem": f"could not fill: {e}"})
        if self.record["needs_you"]:
            self.shot("needs_you")
            raise Escalation(f"{len(self.record['needs_you'])} question(s) need Alex on {step}")

    def upload_resume(self):
        inp = self.page.locator("input[type='file']")
        if inp.count():
            inp.first.set_input_files(str(self.pdf_path))
            self.page.wait_for_timeout(4000)
            self.record["answers"].append({"step": "My Experience", "question": "Resume", "answer": self.pdf_path.name,
                                           "source": "tailored resume"})

    # --- field filling ---------------------------------------------------------------

    def box(self, f):
        return self.page.locator(f"[data-jh-key='{f['key']}']")

    def set_text(self, f, value, source):
        if value is None:
            return
        loc = self.box(f).locator("input, textarea").first
        loc.fill(str(value))
        self.record["answers"].append({"step": self.step_name(), "question": f["label"], "answer": str(value), "source": source})

    def select_options(self, f):
        btn = self.box(f).locator("button[aria-haspopup='listbox']").first
        btn.click()
        self.page.wait_for_timeout(600)
        opts = [t.strip() for t in self.page.locator("[role='listbox'] [role='option']").all_inner_texts() if t.strip()]
        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)
        return opts

    def fill(self, f, reply):
        page, box = self.page, self.box(f)
        kind = f["kind"]
        if kind in ("text", "textarea"):
            box.locator("input, textarea").first.fill(reply)
        elif kind == "select":
            box.locator("button[aria-haspopup='listbox']").first.click()
            page.wait_for_timeout(500)
            page.locator("[role='listbox'] [role='option']").filter(has_text=re.compile(rf"^\s*{re.escape(reply)}\s*$")).first.click()
        elif kind == "multiselect":
            inp = box.locator("input").first
            inp.click()
            inp.fill(reply.split(">")[-1].strip())
            inp.press("Enter")
            page.wait_for_timeout(1500)
            option = page.locator("[data-automation-id='promptOption'], [role='option']").filter(has_text=reply.split(">")[-1].strip())
            option.first.click()
            page.wait_for_timeout(500)
            page.keyboard.press("Escape")
        elif kind == "radio":
            box.get_by_text(reply, exact=True).first.click()
        elif kind == "checkbox":
            cb = box.locator("input[type='checkbox']").first
            cb.check(force=True) if reply == "checked" else cb.uncheck(force=True)
        elif kind == "checkboxes":
            for part in [p.strip() for p in reply.split(";")]:
                box.get_by_text(part, exact=True).first.click()
        elif kind == "date":
            d = date.fromisoformat(reply) if re.match(r"\d{4}-\d{2}-\d{2}", reply) else date.today()
            for sel, val in (("dateSectionMonth-input", f"{d.month:02d}"), ("dateSectionDay-input", f"{d.day:02d}"),
                             ("dateSectionYear-input", str(d.year))):
                part = box.locator(f"[data-automation-id='{sel}']")
                if part.count():
                    part.first.fill(val)
        else:
            raise ValueError(f"unsupported field kind {kind}")
        page.wait_for_timeout(300)

    # --- review / submit -------------------------------------------------------------

    def review(self):
        self.shot("Review")
        self.record["review_text"] = self.page.inner_text("main")[:20000] if self.page.locator("main").count() else ""
        if not self.submit:
            self.record["steps"].append("stopped at Review")
            return "ready"
        self.click(self.next_button())  # labelled Submit on the Review page
        self.settle(4000)
        self.shot("Submitted")
        body = self.page.inner_text("body").lower()
        if "application submitted" in body or "thank you" in body or "successfully" in body:
            self.record["steps"].append("submitted")
            return "submitted"
        self.record["needs_you"].append({"step": "Review", "problem": "Clicked Submit but did not see a confirmation.",
                                         "details": self.errors()})
        raise Escalation("no submission confirmation")

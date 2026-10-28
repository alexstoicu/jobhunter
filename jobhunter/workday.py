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

import yaml

from jobhunter import PRIVATE, answers, vault

NEXT_NAME = re.compile(r"^\s*(Save and Continue|Next|Continue|Submit)\s*$", re.I)

# Fields answered by code, by data-automation-id fragment -> answer bank path.
KNOWN = {  # container ids as seen on Biogen's Workday (formField-<id>)
    "formField-legalName--firstName": "contact.first_name",
    "formField-legalName--lastName": "contact.last_name",
    "formField-preferredName--firstName": "contact.preferred_name",
    "formField-preferredName--lastName": "contact.last_name",
    "formField-addressLine1": "contact.address_line_1",
    "formField-city": "contact.city",
    "formField-postalCode": "contact.postal_code",
    "formField-phoneNumber": "contact.phone",
}

# Fields the Submitter never touches: optional extras, and the LinkedIn profile Alex keeps off applications.
LEAVE_ALONE = {"formField-preferredCheck", "formField-skills", "formField-linkedInAccount",
               "formField-gradeAverage", "formField-fieldOfStudy", "formField-websiteUrl",
               # filled by fill_experience from the claim bank
               "formField-jobTitle", "formField-companyName", "formField-location", "formField-currentlyWorkHere",
               "formField-startDate", "formField-endDate", "formField-roleDescription", "formField-schoolName",
               "formField-degree"}

# Degree wording differs per tenant; the first option containing one of these words wins.
DEGREE_WORDS = {"msc": ("master", "m.s", "msc", "m.sc", "ms "), "bsc": ("bachelor", "b.s", "bsc", "b.sc", "bs ")}
WORK_ENTRIES = 10  # work history entries to enter, newest first

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
        screen = json.loads(job["screen"] or "{}")
        self.salary = screen.get("apply_figure")
        self.record = {"steps": [], "answers": [], "needs_you": [], "skipped": [], "screens": [], "account": None}
        self.reloads = 0

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
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # some tenants never go quiet (tracking calls); the fixed wait below is enough
        self.page.wait_for_timeout(ms)

    def wait_for_form(self, timeout=45):
        """Wait until the step's fields have rendered and the loading spinner is gone.

        Workday sometimes shows "Something went wrong" when re-entering a saved draft; a reload fixes it.
        """
        for _ in range(timeout * 2):
            if self.recover():
                continue
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
        self.shot("not_loading")
        raise Escalation(f"the {self.step_name() or 'form'} page didn't finish loading")

    def recover(self):
        """Reload when Workday shows its "Something went wrong" page (common on saved drafts)."""
        if not self.page.get_by_text("Something went wrong").count():
            return False
        if self.reloads >= 4:
            raise Escalation("Workday keeps showing 'Something went wrong'")
        self.reloads += 1
        self.log("Workday error page; reloading")
        self.page.reload(wait_until="networkidle")
        self.page.wait_for_timeout(4000)
        return True

    def next_button(self):
        return self.page.get_by_role("button", name=NEXT_NAME).last

    def step_name(self):
        loc = self.page.locator("[data-automation-id='progressBarActiveStep']")
        return re.sub(r"^current step \d+ of \d+\s*", "", loc.first.inner_text().strip()) if loc.count() else ""

    def errors(self):
        loc = self.page.locator("[data-automation-id='errorMessage'], [data-automation-id='inputErrorMessage'], "
                                "[data-automation-id='errorBanner'] li, [role='alert']")
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

        for _ in range(16):  # pages in the flow, with headroom for reloads
            self.wait_for_form()
            step = self.step_name()
            self.log(f"step: {step}")
            if step.lower().startswith("review"):
                return self.review()
            self.fill_page(step)
            if self.recover():
                continue  # the page broke while filling; start this step again
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

    def signed_in(self):
        """Past the account step. The progress bar also shows on the sign-in page, as "Create Account/Sign In"."""
        if not self.page.locator("[data-automation-id='progressBarActiveStep']").count():
            return False
        return not re.search(r"create account|sign in", self.step_name(), re.I)

    def visible(self, *locators):
        """The first of the locators that is on screen, or None."""
        for loc in locators:
            if loc.count() and loc.first.is_visible():
                return loc.first
        return None

    def email_box(self):
        return self.visible(self.page.locator("[data-automation-id='email']"),
                            self.page.get_by_label(re.compile("^email", re.I)))

    def password_box(self):
        return self.visible(self.page.locator("[data-automation-id='password']"),
                            self.page.get_by_label(re.compile("^password", re.I)))

    def sign_in_form(self):
        """Fill and submit the Sign In form (the account already exists)."""
        page = self.page
        link = self.visible(page.locator("[data-automation-id='signInLink']"),
                            page.get_by_role("link", name=re.compile("^sign in$", re.I)))
        if link and not page.locator("[data-automation-id='signInSubmitButton']").count():
            link.click()
            self.settle(1000)
        self.email_box().fill(_get(self.bank, "contact.email"))
        self.password_box().fill(self.password)
        self.click(self.visible(page.locator("[data-automation-id='signInSubmitButton']"),
                                page.get_by_role("button", name=re.compile("^sign in$", re.I))))
        self.record["account"] = "signed in"

    def create_account_form(self):
        page = self.page
        link = self.visible(page.locator("[data-automation-id='createAccountLink']"),
                            page.get_by_role("link", name=re.compile("^create account$", re.I)),
                            page.get_by_role("button", name=re.compile("^create account$", re.I)))
        if link:
            link.click()
            self.settle(1000)
        self.email_box().fill(_get(self.bank, "contact.email"))
        self.password_box().fill(self.password)
        verify = page.locator("[data-automation-id='verifyPassword']")
        if verify.count():
            verify.first.fill(self.password)
        box = page.locator("[data-automation-id='createAccountCheckbox']")
        if box.count():
            box.first.check(force=True)
        self.click(self.visible(page.locator("[data-automation-id='createAccountSubmitButton']"),
                                page.get_by_role("button", name=re.compile("^create account$", re.I))))
        self.record["account"] = "created"

    def account_state(self, timeout=25):
        """Wait until the page settles: "in" (signed in), "form" (a sign-in form), "verify" or "unknown"."""
        for _ in range(timeout * 2):
            if self.signed_in():
                return "in"
            body = self.page.inner_text("body").lower()
            if "verify your" in body or "verification email" in body:
                return "verify"
            if self.password_box():
                return "form"
            self.page.wait_for_timeout(500)
        return "unknown"

    def sign_in(self):
        page = self.page
        self.password, created = vault.get(self.host)
        self.settle(1500)
        if self.signed_in():
            self.record["account"] = "already signed in"
            return
        entry = self.visible(page.locator("[data-automation-id='utilityButtonSignIn']"))
        if entry and not self.email_box():
            entry.click()
            self.settle(1000)
        if created:
            self.create_account_form()
        else:
            self.sign_in_form()
        self.settle(2000)
        state = self.account_state()
        # A saved password with no account behind it (an earlier attempt never created one): create the account now.
        if state == "form" and not created:
            self.create_account_form()
            created = True
            self.settle(2000)
            state = self.account_state()
        # Some sites send you back to Sign In after creating the account; sign in with the new password.
        if state == "form" and created and not self.errors():
            self.sign_in_form()
            self.record["account"] = "created, then signed in"
            self.settle(2000)
            state = self.account_state()
        if state != "in":
            self.shot("account")
            errs = self.errors()
            why = ("The site wants the email address verified first: open the verification email from this "
                   "employer, click its link, then tap Approve again." if state == "verify" or (
                       created and any("wrong email" in e.lower() for e in errs)) else
                   "The Submitter couldn't get past this employer's sign-in page.")
            self.record["needs_you"].append({"step": "Account", "problem": why, "details": errs})
            raise Escalation("account step needs attention")

    def fill_page(self, step):
        """Fill the page; then fill again for questions that appeared because of earlier answers."""
        if "experience" in step.lower():
            self.upload_resume()
            self.fill_experience()
        seen = set()
        for _ in range(3):
            labels = self.fill_fields(step, seen)
            if not labels:
                break
            seen |= labels
            self.page.wait_for_timeout(800)

    def fill_fields(self, step, seen):
        """One pass over the visible fields not handled yet. Returns the fields handled (id, label)."""
        page = self.page
        fields = [f for f in page.evaluate(EXTRACT_JS) if (f["id"], f["label"]) not in seen]
        handled = {(f["id"], f["label"]) for f in fields}
        self.record.setdefault("fields", []).extend(
            {"step": step, "id": f["id"], "label": f["label"][:60], "kind": f["kind"], "value": f["value"][:40]} for f in fields)
        todo = []
        for f in fields:
            if f["kind"] == "file" or f["id"] in LEAVE_ALONE:
                continue
            known = KNOWN.get(f["id"])
            if known and f["kind"] == "text":
                self.set_text(f, _get(self.bank, known), source=known)
            elif f["value"] and f["kind"] not in ("checkbox",) and f["value"].lower() not in ("select one", ""):
                continue  # already filled (e.g. by Workday from the account)
            else:
                if f["kind"] == "select":
                    f["options"] = self.select_options(f)
                todo.append(f)
        if not todo:
            return handled
        questions = [{k: f[k] for k in ("key", "label", "kind", "options", "required")} for f in todo]
        mapped = answers.answer(self.conn, questions, self.job, self.salary)
        for f in todo:
            reply, source = mapped.get(f["key"], (None, "none"))
            if reply is None:
                entry = {"step": step, "question": f["label"], "kind": f["kind"], "options": f["options"]}
                self.record["needs_you" if f["required"] else "skipped"].append(entry)
                continue
            try:
                self.fill(f, reply)
                self.record["answers"].append({"step": step, "question": f["label"], "answer": reply, "source": source})
            except Exception as e:
                self.record["needs_you"].append({"step": step, "question": f["label"], "problem": f"could not fill: {e}"})
        if self.record["needs_you"]:
            self.shot("needs_you")
            raise Escalation(f"{len(self.record['needs_you'])} question(s) need Alex on {step}")
        return handled

    def upload_resume(self):
        page, name = self.page, self.pdf_path.name
        attached = page.get_by_text(name, exact=True)
        # Re-runs on a saved draft must not attach the resume again; remove duplicates left earlier.
        while attached.count() > 1:
            page.locator("[data-automation-id='delete-file']").last.click(force=True)
            page.wait_for_timeout(1500)
        inp = page.locator("input[type='file']")
        if inp.count() and not attached.count():
            inp.first.set_input_files(str(self.pdf_path))
            page.wait_for_timeout(4000)
            self.record["answers"].append({"step": "My Experience", "question": "Resume", "answer": self.pdf_path.name,
                                           "source": "tailored resume"})

    # --- My Experience: work history and education from the claim bank --------------------

    def _claims(self):
        with open(PRIVATE / "claims.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _month_year(self, box, iso):
        """Fill a Workday month/year date box from "2024-09"."""
        year, month = iso.split("-")[:2]
        for sel, val in (("dateSectionMonth-input", month), ("dateSectionYear-input", year)):
            part = box.locator(f"[data-automation-id='{sel}']")
            if part.count():
                part.first.fill(val)

    def _add_after(self, anchor):
        """Click the Add button that follows the anchor (a heading or the last entry's field)."""
        anchor.locator("xpath=following::button[@data-automation-id='add-button'][1]").first.click(force=True)
        self.page.wait_for_timeout(1200)

    def fill_experience(self):
        page = self.page
        bank = self._claims()
        titles = page.locator("[data-automation-id='formField-jobTitle']")
        roles = [r for r in bank["roles"] if r.get("employer")][:WORK_ENTRIES]
        if titles.count() and titles.first.locator("input").first.input_value():
            self._end_dates(roles)  # entries already there (saved draft, or Workday read them from the resume)
            return
        for i, r in enumerate(roles):
            if i == 0:  # Work Experience comes first on the page, so its Add button is the first one
                page.locator("[data-automation-id='add-button']").first.click(force=True)
                page.wait_for_timeout(1200)
            else:
                self._add_after(page.locator("[data-automation-id='formField-roleDescription']").nth(i - 1))
            nth = lambda name: page.locator(f"[data-automation-id='formField-{name}']").nth(i)
            nth("jobTitle").locator("input").first.fill(r["title"])
            nth("companyName").locator("input").first.fill(re.sub(r"\s*\(.*\)", "", r["employer"]))
            if r.get("location"):
                nth("location").locator("input").first.fill(r["location"])
            current = str(r["end"]).lower() == "present"
            if current:
                cb = nth("currentlyWorkHere").locator("input[type='checkbox']").first
                if not cb.is_checked():
                    cb.click(force=True)
                    page.wait_for_timeout(500)
            self._month_year(nth("startDate"), str(r["start"]))
            desc = "\n".join("- " + b["text"] for b in r["bullets"])[:1800]
            nth("roleDescription").locator("textarea").first.fill(desc)
            self.record["answers"].append({"step": "My Experience", "question": f"Work experience: {r['title']}",
                                           "answer": f"{r['employer']}, {r['start']} to {r['end']}",
                                           "source": "claim bank"})
        self._end_dates(roles)
        self.fill_education(bank)

    def _end_dates(self, roles):
        """End dates. A current job has no "To" box, so the boxes line up with the past jobs only."""
        ends = self.page.locator("[data-automation-id='formField-endDate']")
        past = [r for r in roles if str(r["end"]).lower() != "present"]
        for j, r in enumerate(past[:ends.count()]):
            self._month_year(ends.nth(j), str(r["end"]))

    def pick_degree(self, box, degree):
        want = DEGREE_WORDS["msc" if degree.lower().startswith("m") else "bsc"]
        box.locator("button[aria-haspopup='listbox']").first.click()
        self.page.wait_for_timeout(600)
        options = self.page.locator("[role='listbox'] [role='option']")
        texts = [t.strip() for t in options.all_inner_texts()]
        for j, t in enumerate(texts):
            if any(w in (t.lower() + " ") for w in want):
                options.nth(j).click()
                return t
        self.page.keyboard.press("Escape")
        raise ValueError(f"no degree option matched {degree}; options: {texts[:12]}")

    def fill_education(self, bank):
        page = self.page
        schools = page.locator("[data-automation-id='formField-schoolName']")
        if schools.count() and schools.first.locator("input").first.input_value():
            return
        for i, e in enumerate(bank["education"]):
            if i:
                self._add_after(schools.nth(i - 1))
            nth = lambda name: page.locator(f"[data-automation-id='formField-{name}']").nth(i)
            nth("schoolName").locator("input").first.fill(e["school"])
            chosen = self.pick_degree(nth("degree"), e["degree"])
            self.record["answers"].append({"step": "My Experience", "question": f"Education: {e['degree']}",
                                           "answer": f"{e['school']} ({chosen})", "source": "claim bank"})

    # --- field filling ---------------------------------------------------------------

    def box(self, f):
        """The field's container. Workday re-renders on input and drops our tag; find the field again by id and label."""
        loc = self.page.locator(f"[data-jh-key='{f['key']}']")
        if loc.count():
            return loc
        for g in self.page.evaluate(EXTRACT_JS):
            if g["id"] == f["id"] and g["label"] == f["label"]:
                f["key"] = g["key"]
                break
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
            want = reply.lower().startswith(("checked", "yes", "i agree", "true"))
            if cb.is_checked() != want:  # click only to change state; Workday checkboxes toggle on any click
                cb.click(force=True)
                page.wait_for_timeout(400)
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

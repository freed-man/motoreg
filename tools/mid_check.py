#!/usr/bin/env python3
"""
mid_check.py - check whether your own vehicle(s) show as insured on the
Motor Insurance Database via MIB's free "Check Your Vehicle" service.

Usage
-----
    python mid_check.py AB12CDE
    python mid_check.py AB12CDE CD34EFG --json results.json
    python mid_check.py --file regs.txt            # one registration per line
    python mid_check.py --inspect                   # show the page's controls, run no check
    python mid_check.py AB12CDE --channel chrome    # drive your installed Chrome instead

One-time setup
--------------
    pip install playwright
    playwright install chromium      # not needed if you use --channel chrome / msedge

How it works
------------
It opens a *visible* browser, clicks through "Personal check", ticks the
declaration/terms boxes, enters your registration, submits, and reads the
result. Every result is screenshotted into ./mid_results/ so you can verify it.

If the site shows a captcha / "verify you are human" step, or the script can't
find the next control, it stops and asks you to do that bit in the browser
window, then carries on when you press Enter. It does not try to get around
the site's bot checks - that's deliberate.

Exit codes: 0 = every vehicle insured, 2 = at least one not insured,
            3 = at least one unknown/error result.
--json writes one record per vehicle: reg, status (INSURED / NOT_INSURED /
ERROR / UNKNOWN), detail, make_model, site_time, checked_at, screenshot.

Before you use it
-----------------
* Only check vehicles you own or are entitled to drive. That is the
  service's condition of use, and MIB logs every check.
* Read the terms shown on the site once yourself; the script ticks the
  acceptance box on your behalf.
* MIB rate-limits the free check. Keep the list short and don't run this on
  a tight schedule (the --delay between vehicles defaults to 8 seconds).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("Playwright is not installed. Run:  pip install playwright && playwright install chromium")

DEFAULT_URL = "https://enquiry.navigate.mib.org.uk/checkyourvehicle"
DEFAULT_PROFILE = Path.home() / ".mid_check" / "browser-profile"
DEFAULT_OUT = Path("mid_results")

I = re.IGNORECASE
REG_RE = re.compile(r"^[A-Z0-9]{2,8}$")

# --------------------------------------------------------------------------
# What the script looks for on the page. If MIB changes the site, run
# --inspect and adjust these.
# --------------------------------------------------------------------------
PERSONAL_CHECK = re.compile(r"personal\s+check", I)
DECLARATION_LABEL = re.compile(
    r"terms|condition|accept|agree|confirm|declar|understand|own|keeper|entitled|permit|legal|allowed", I
)
CONTINUE_BUTTON = re.compile(r"^\s*(continue|next|proceed|start|begin|get started|accept|i agree|agree)\b", I)
COOKIE_BUTTON = re.compile(
    r"^\s*(accept|allow)(\s+(all|essential|necessary|recommended))?(\s+cookies)?\s*$|^\s*(ok|okay|got it)\s*$", I
)
SUBMIT_BUTTON = re.compile(r"check\s*(this|my|your)?\s*vehicle|check\s*now|^\s*(check|search|submit)\s*$", I)
REG_FIELD = re.compile(r"registration\s*(number|no\.?|mark)?|\bvrn\b|\bvrm\b|number\s*plate|\breg\b", I)

# The real result page (Sept 2026): "This vehicle is showing as  INSURED  in Navigate today".
RESULT_CONTAINER = "[data-testid='VRNResultPage']"
RESULT_STATUS_RE = re.compile(r"this vehicle is showing as\s+(.+?)\s+in navigate today", I)

# Static sentences that mention insurance and would fool the generic patterns below.
BOILERPLATE = [
    r"if your vehicle is not showing as insured,? keep your policy details with you until navigate is updated\.?",
    r"this check only confirms if the vehicle is currently showing as insured\.?",
    r"this check is not proof of insurance status\.?",
    r"in the event of an accident and you need to check someone else'?s vehicle is insured.*?look-?up service\.?",
]

# Generic result wording (fallback if the page structure changes). Checked in this order; first match wins.
ERROR_PATTERNS = [
    r"enter a valid (vehicle )?registration",
    r"invalid (vehicle )?registration",
    r"registration( number)? (is )?(not valid|invalid)",
    r"exceeded the (number|limit)",
    r"too many (requests|checks|searches)",
    r"revisit (the site|later)",
    r"try again later",
    r"currently unavailable",
    r"something went wrong",
    r"an error (has )?occurred",
]
NOT_INSURED_PATTERNS = [
    r"no live insurance",
    r"not (currently |been )?(insured|found|showing)",
    r"uninsured",
    r"does not appear",
    r"no (valid |current |active )?(insurance )?polic(y|ies)",
    r"could not (be )?found",
    r"not on (the )?(motor insurance database|mid)\b",
]
INSURED_STRONG = [
    r"live insurance policy",
    r"currently insured",
    r"(is|shows?|showing)( as)? insured",
    r"vehicle is insured",
    r"insured on (the )?(motor insurance database|mid)\b",
]
INSURED_WEAK = [r"\binsured\b(?!\s+vehicles)"]  # only trusted on text that appeared after submitting

BUSY_PATTERN = re.compile(r"\b(processing|loading|please wait|one moment)\b", I)
CHALLENGE_TEXT = [
    r"verify (that )?you are (a )?human",
    r"i'?m not a robot",
    r"checking your browser",
    r"access denied",
    r"request (was |has been )?blocked",
    r"unusual (traffic|activity)",
]
CHALLENGE_FRAMES = "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], iframe[src*='challenges.cloudflare.com']"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def pause(msg: str) -> None:
    print(f"\n>>> {msg}\n>>> When you're done, press Enter here to continue...", flush=True)
    input()


def body_text(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def settle(page: Page, secs: float = 0.8) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PWTimeout:
        pass
    time.sleep(secs)


def first_visible(locator):
    """Return the first visible element matched by a locator, or None."""
    try:
        n = locator.count()
    except Exception:
        return None
    for i in range(n):
        el = locator.nth(i)
        try:
            if el.is_visible():
                return el
        except Exception:
            pass
    return None


def click_first(candidates) -> bool:
    for loc in candidates:
        el = first_visible(loc)
        if el is None:
            continue
        try:
            el.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def is_text_input(el) -> bool:
    try:
        return bool(
            el.evaluate(
                "e => (e.tagName === 'INPUT' && ['', 'text', 'search'].includes((e.getAttribute('type') || '').toLowerCase()))"
                " || e.tagName === 'TEXTAREA'"
            )
        )
    except Exception:
        return False


def challenge_present(page: Page) -> str | None:
    if first_visible(page.locator(CHALLENGE_FRAMES)) is not None:
        return "a captcha / human-verification widget is showing"
    text = " ".join(body_text(page).split())
    for pat in CHALLENGE_TEXT:
        if re.search(pat, text, I):
            return f"the page says something like '{pat}'"
    return None


def dump_controls(page: Page) -> None:
    """Print the page's interactive elements - used by --inspect and as a fallback."""
    js = """() => {
      const sel = 'button, a[href], input, select, textarea, [role=button], [role=radio], [role=checkbox], [role=tab]';
      const out = [];
      for (const e of document.querySelectorAll(sel)) {
        const r = e.getBoundingClientRect();
        if (!r.width && !r.height) continue;
        const lab = (e.labels && e.labels[0]) ? e.labels[0].innerText : '';
        const text = (e.innerText || e.value || e.placeholder || lab || e.getAttribute('aria-label') || '').trim();
        out.push({
          tag: e.tagName.toLowerCase(),
          type: e.getAttribute('role') || e.getAttribute('type') || '',
          id: e.id || '', name: e.getAttribute('name') || '',
          testid: e.getAttribute('data-testid') || '',
          text: text.replace(/\\s+/g, ' ').slice(0, 80),
          checked: (typeof e.checked === 'boolean') ? String(e.checked) : (e.getAttribute('aria-checked') || ''),
          disabled: (e.disabled || e.getAttribute('aria-disabled') === 'true') ? 'yes' : ''
        });
      }
      return out;
    }"""
    print(f"\n--- controls on {page.url} ---")
    try:
        for c in page.evaluate(js):
            bits = [c["tag"]]
            if c["type"]:
                bits.append(f"type={c['type']}")
            if c["id"]:
                bits.append(f"id={c['id']}")
            if c["name"]:
                bits.append(f"name={c['name']}")
            if c["testid"]:
                bits.append(f"testid={c['testid']}")
            if c["checked"]:
                bits.append(f"checked={c['checked']}")
            if c["disabled"]:
                bits.append("DISABLED")
            print(f"  {' '.join(bits):60s} {c['text']!r}")
    except Exception as exc:
        print(f"  (could not list controls: {exc})")
    frames = [f.url for f in page.frames if f != page.main_frame and f.url]
    if frames:
        print("  embedded frames:")
        for u in frames:
            print(f"    {u[:100]}")
    print("--- end ---")


# --------------------------------------------------------------------------
# Walking the form
# --------------------------------------------------------------------------
def find_reg_field(page: Page):
    candidates = [
        page.get_by_test_id("vrm_searchtext"),   # the real site (Sept 2026): <input id="vrm" name="vrm">
        page.get_by_label(REG_FIELD),
        page.get_by_placeholder(REG_FIELD),
        page.locator(
            "input[name*='vrn' i], input[id*='vrn' i], input[name*='vrm' i], input[id*='vrm' i], "
            "input[name*='reg' i], input[id*='reg' i]"
        ),
        page.locator("input[type='text'], input:not([type])"),
    ]
    for loc in candidates:
        try:
            n = loc.count()
        except Exception:
            continue
        for i in range(n):
            el = loc.nth(i)
            try:
                if el.is_visible() and is_text_input(el):
                    return el
            except Exception:
                pass
    return None


def tick_declarations(page: Page) -> bool:
    """Tick unchecked boxes whose label reads like a terms/declaration statement."""
    ticked = False
    # The real site (Sept 2026): a Radix checkbox button, "I confirm that I have read and accept the Terms & Conditions."
    # Click the box itself - the "Terms & Conditions." text next to it is a PDF download button.
    terms = first_visible(page.locator("button[role='checkbox'][value='termsAccepted']"))
    if terms is not None:
        try:
            if not terms.is_checked():
                terms.click(timeout=5000)
                ticked = True
        except Exception:
            pass
    boxes = page.get_by_role("checkbox")
    try:
        n = boxes.count()
    except Exception:
        return ticked
    for i in range(n):
        cb = boxes.nth(i)
        try:
            if cb.get_attribute("aria-readonly") == "true" or cb.is_checked():
                continue  # read-only ones are the VRN page's validation indicators, not declarations
            label = cb.evaluate(
                "e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label')"
                " || (e.closest('label') && e.closest('label').innerText)"
                " || (e.parentElement && e.parentElement.innerText) || '').trim()"
            )
        except Exception:
            continue
        if label and not DECLARATION_LABEL.search(label):
            print(f"  leaving unticked (doesn't look like a declaration): {label[:70]!r}")
            continue
        try:
            cb.check(timeout=3000)
        except Exception:
            try:  # visually-hidden native input: click its label instead
                cb.evaluate("e => ((e.labels && e.labels[0]) || e).click()")
            except Exception:
                continue
        ticked = True
    return ticked


def select_personal_check(page: Page) -> bool:
    """Select the 'Personal check' card. Returns False if it's already selected or not on this screen."""
    candidates = [
        page.get_by_test_id("personal_check_card"),   # the real site (Sept 2026): a role=radio card
        page.get_by_role("radio", name=PERSONAL_CHECK),
        page.get_by_role("button", name=PERSONAL_CHECK),
        page.get_by_role("link", name=PERSONAL_CHECK),
        page.get_by_label(PERSONAL_CHECK),
    ]
    for loc in candidates:
        el = first_visible(loc)
        if el is None:
            continue
        try:
            if el.get_attribute("aria-checked") == "true" or el.get_attribute("data-state") == "checked":
                return False
            el.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def click_continue(page: Page) -> bool:
    """Click Continue/Next/Accept. The site keeps Continue disabled until a choice is made, so wait a moment for it."""
    candidates = [
        page.get_by_test_id("continueBtn"),                                        # landing page (Sept 2026)
        page.get_by_role("button", name=re.compile(r"^\s*agree and continue\s*$", I)),  # terms page (Sept 2026)
        page.get_by_role("button", name=CONTINUE_BUTTON),
        page.get_by_role("link", name=CONTINUE_BUTTON),
    ]
    for loc in candidates:
        el = first_visible(loc)
        if el is None:
            continue
        for _ in range(10):  # up to ~3s for it to become enabled
            try:
                if el.is_enabled():
                    break
            except Exception:
                break
            time.sleep(0.3)
        else:
            continue  # still disabled: nothing to click yet
        try:
            el.click(timeout=5000)
            return True
        except Exception:
            continue
    return False


def reach_reg_form(page: Page, max_steps: int = 10):
    """Click through the intro/declaration screens until the registration box is visible."""
    stalled = 0
    last_state = None
    for step in range(1, max_steps + 1):
        why = challenge_present(page)
        if why:
            pause(f"The site wants a human check ({why}). Please complete it in the browser window.")
            settle(page)
            continue

        field = find_reg_field(page)
        if field is not None:
            return field

        did = []
        if click_first([page.get_by_role("button", name=COOKIE_BUTTON)]):
            did.append("cookie banner")
        if tick_declarations(page):
            did.append("ticked declaration(s)")
        if select_personal_check(page):
            did.append("chose 'Personal check'")
        if click_continue(page):
            did.append("clicked continue")

        settle(page)
        state = (page.url, body_text(page))
        if did and state != last_state:
            stalled = 0
            print(f"  step {step}: {', '.join(did)}")
        else:
            stalled += 1
        last_state = state

        if stalled >= 2:
            dump_controls(page)
            pause(
                "I couldn't get to the registration box on my own. Please click through to it in the "
                "browser window (the controls I can see are listed above - the regexes at the top of "
                "the script may need adjusting)."
            )
            stalled = 0

    field = find_reg_field(page)
    if field is None:
        raise RuntimeError("never reached the registration form (try --inspect, or --channel chrome)")
    return field


def submit_reg(page: Page, field, reg: str) -> None:
    field.click()
    field.fill(reg)
    # The real site shows read-only ticks once the VRN passes its checks. If they haven't lit up,
    # the React input didn't register the fill, so type it key by key instead.
    indicator = first_visible(page.get_by_test_id("checkNumLetter"))
    if indicator is not None:
        for _ in range(10):
            if indicator.get_attribute("aria-checked") == "true":
                break
            time.sleep(0.2)
        else:
            field.fill("")
            field.press_sequentially(reg, delay=60)
            time.sleep(0.5)
    if not click_first(
        [
            page.get_by_test_id("continueBtn"),                       # "Check Your Vehicle" (Sept 2026)
            page.get_by_role("button", name=SUBMIT_BUTTON),
            page.locator("button[type='submit']"),
            page.locator("input[type='submit']"),
        ]
    ):
        field.press("Enter")


def read_result_page(page: Page) -> dict | None:
    """Read the site's result page by structure. Returns None if it isn't showing."""
    box = first_visible(page.locator(RESULT_CONTAINER))
    if box is None:
        return None
    try:
        text = box.inner_text(timeout=3000)
    except Exception:
        return None
    m = RESULT_STATUS_RE.search(" ".join(text.split()))
    if not m:
        return None
    site_status = m.group(1).strip()
    s = site_status.lower()
    if re.search(r"\bnot\b|\bun-?insured\b|\bno\b", s):
        status = "NOT_INSURED"
    elif re.fullmatch(r"insured\W*", s):
        status = "INSURED"
    else:
        status = "UNKNOWN"
    info = {"status": status, "site_status": site_status}
    flat = " ".join(text.split())
    v = re.search(r"vehicle registration number:?\s*([A-Z0-9 ]{2,10}?)\s+make and model", flat, I)
    if v:
        info["site_vrn"] = v.group(1).strip()
    mm = re.search(r"make and model:?\s*(.+?)(?=\s+(?:it may take|this check|exit to mib)|$)", flat, I)
    if mm:
        info["make_model"] = mm.group(1).strip()
    t = re.search(r"\d{1,2}:\d{2}:\d{2}\s+\d{1,2}\s+\w+\s+\d{4}", text)
    if t:
        info["site_time"] = t.group(0)
    return info


def wait_for_result(page: Page, before_text: str, before_url: str, timeout: float) -> str:
    """Wait until the result page is up, or the page has changed after submit and stopped changing."""
    deadline = time.time() + timeout
    changed, prev, stable = False, before_text, 0
    while time.time() < deadline:
        time.sleep(0.7)
        why = challenge_present(page)
        if why:
            pause(f"A human check appeared after submitting ({why}). Please complete it in the browser window.")
            deadline = time.time() + timeout
            continue
        if read_result_page(page) is not None:
            time.sleep(0.5)
            return body_text(page)
        now = body_text(page)
        if page.url != before_url or now != before_text:
            changed = True
        if not changed:
            continue
        stable = stable + 1 if now == prev else 0
        prev = now
        if stable >= 2 and not BUSY_PATTERN.search(now):
            return now
    return body_text(page)


def classify(after: str, before: str) -> tuple[str, str]:
    """Return (status, snippet). Looks at text that is new since submitting first."""
    before_lines = {ln.strip() for ln in before.splitlines()}
    new_text = "\n".join(ln for ln in after.splitlines() if ln.strip() and ln.strip() not in before_lines)

    def snippet(text: str, m: re.Match) -> str:
        return text[max(0, m.start() - 60): m.end() + 80].strip()

    for text, allow_weak in ((new_text, True), (after, False)):
        flat = " ".join(text.split())
        for pat in BOILERPLATE:
            flat = re.sub(pat, " ", flat, flags=I)
        if not flat.strip():
            continue
        groups = [
            ("ERROR", ERROR_PATTERNS),
            ("NOT_INSURED", NOT_INSURED_PATTERNS),
            ("INSURED", INSURED_STRONG + (INSURED_WEAK if allow_weak else [])),
        ]
        for status, patterns in groups:
            for pat in patterns:
                m = re.search(pat, flat, I)
                if m:
                    return status, snippet(flat, m)
    return "UNKNOWN", " ".join(new_text.split())[:300]


# --------------------------------------------------------------------------
# One vehicle
# --------------------------------------------------------------------------
def check_one(page: Page, reg: str, url: str, out_dir: Path, timeout: float) -> dict:
    stamp = datetime.now()
    result = {
        "reg": reg,
        "status": "ERROR",
        "detail": "",
        "checked_at": stamp.isoformat(timespec="seconds"),
        "screenshot": "",
    }
    print(f"\n=== {reg} ===")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        settle(page)
        field = reach_reg_form(page)
        before_text, before_url = body_text(page), page.url
        submit_reg(page, field, reg)
        after = wait_for_result(page, before_text, before_url, timeout)
        info = read_result_page(page)
        if info is not None:
            bits = [info["site_status"]]
            if info.get("make_model"):
                bits.append(info["make_model"])
            if info.get("site_time"):
                bits.append(f"as of {info['site_time']}")
            result.update(status=info["status"], detail=" | ".join(bits),
                          make_model=info.get("make_model", ""), site_time=info.get("site_time", ""))
            if info.get("site_vrn") and info["site_vrn"].replace(" ", "").upper() != reg:
                result["detail"] += f" | WARNING: page shows {info['site_vrn']}"
        else:
            status, detail = classify(after, before_text)
            result.update(status=status, detail=detail)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result["detail"] = f"{type(exc).__name__}: {exc}"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        shot = out_dir / f"{reg}_{stamp:%Y%m%d_%H%M%S}.png"
        page.screenshot(path=str(shot), full_page=True)
        result["screenshot"] = str(shot)
    except Exception:
        pass
    print(f"-> {result['status']}   {result['detail']}")
    return result


def inspect_loop(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    settle(page)
    while True:
        dump_controls(page)
        ans = input("\nNavigate in the browser, then press Enter to list the controls again (q to quit): ")
        if ans.strip().lower() == "q":
            return


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def load_regs(args) -> list[str]:
    raw = list(args.regs)
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                raw.append(line)
    regs = []
    for r in raw:
        clean = re.sub(r"[\s-]+", "", r).upper()
        if not REG_RE.match(clean) or not re.search(r"[A-Z]", clean) or not re.search(r"\d", clean):
            print(f"skipping {r!r}: the site wants 2-8 characters using both letters and numbers")
            continue
        if clean not in regs:
            regs.append(clean)
    return regs


def main() -> int:
    ap = argparse.ArgumentParser(description="Check your own vehicle(s) on the Motor Insurance Database.")
    ap.add_argument("regs", nargs="*", help="registration(s), e.g. AB12CDE")
    ap.add_argument("--file", help="text file with one registration per line")
    ap.add_argument("--json", help="write results to this JSON file")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="folder for screenshots (default: mid_results)")
    ap.add_argument("--profile", default=str(DEFAULT_PROFILE), help="browser profile folder (keeps cookies between runs)")
    ap.add_argument("--channel", help="use an installed browser: chrome or msedge")
    ap.add_argument("--url", default=DEFAULT_URL, help="check page URL (also try https://checkyourvehicle.org.uk/checkyourvehicle)")
    ap.add_argument("--delay", type=float, default=8.0, help="seconds to wait between vehicles (default 8)")
    ap.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for a result (default 30)")
    ap.add_argument("--inspect", action="store_true", help="just open the page and list its controls")
    ap.add_argument("--keep-open", action="store_true", help="leave the browser open at the end")
    args = ap.parse_args()

    regs = [] if args.inspect else load_regs(args)
    if not args.inspect and not regs:
        ap.error("give at least one registration, or --file, or --inspect")

    print("Reminder: only check vehicles you own or are entitled to drive. MIB logs every lookup.")
    results: list[dict] = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(args.profile),
            headless=False,
            channel=args.channel,
            viewport={"width": 1200, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            if args.inspect:
                inspect_loop(page, args.url)
                return 0
            for i, reg in enumerate(regs):
                if i:
                    time.sleep(args.delay)
                tab = ctx.new_page()  # fresh tab: the site keeps its step-by-step state client-side
                try:
                    results.append(check_one(tab, reg, args.url, Path(args.out), args.timeout))
                finally:
                    if not args.keep_open:
                        tab.close()
        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            if args.keep_open:
                pause("Browser left open for you to look at.")
            ctx.close()

    if not results:
        return 3
    print("\n==== summary ====")
    for r in results:
        print(f"  {r['reg']:<10} {r['status']:<12} {r['detail'][:90]}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2))
        print(f"written {args.json}")

    statuses = {r["status"] for r in results}
    if statuses & {"UNKNOWN", "ERROR"}:
        return 3
    if "NOT_INSURED" in statuses:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

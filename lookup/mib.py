"""
The insurance check itself: walks MIB's "Check Your Vehicle" site the way
you would by hand, and reads the answer off the result screen.

This is check-insurance.mjs in Python, for the site to run headless. It
ticks the terms box on your behalf, as that script does once you have read
them. Nothing here tries to get past a human check: if the site shows one,
or turns the server away, the check stops and says so.
"""

import re

START_URL = 'https://checkyourvehicle.org.uk/checkyourvehicle'
STEP_MS = 20_000      # time allowed for the site to move from one screen to the next
RESULT_MS = 40_000    # time allowed for the result after the registration is sent

HEADLINE = re.compile(
    r'This vehicle is showing as\s+(.{1,40}?)\s+in Navigate today', re.I)
SITE_TIME = re.compile(r'\b\d{1,2}:\d{2}:\d{2}\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b')
COOKIE_BUTTON = re.compile(
    r'^\s*(accept|allow)(\s+(all|essential|necessary|recommended))?(\s+cookies)?\s*$'
    r'|^\s*(ok|okay|got it)\s*$', re.I)
CHALLENGE_FRAMES = (
    "iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile'], "
    "iframe[src*='challenges.cloudflare.com']")
CHALLENGE_TEXT = re.compile(
    r"verify(ing)? (that )?you are (a )?human|i'?m not a robot|checking your browser"
    r'|unusual (traffic|activity)', re.I)
HUMAN_CHECK = (
    'The site wants a human check '
    '(a captcha or "verify you are human" screen is showing)')

# The text and the form values of the screen, without the page furniture
READ_SCREEN = """() => {
    const main = document.querySelector('main') ?? document.body;
    return {
        text: main.innerText,
        fields: [...main.querySelectorAll('input')].map((el) => el.value),
    };
}"""

# True once the result is up, or the registration screen has gone
RESULT_OR_MOVED_ON = """() => {
    const text = (document.querySelector('main') ?? document.body).innerText;
    return /This vehicle is showing as/i.test(text) || !text.includes('Enter your VRN');
}"""


class Stopped(Exception):
    """The check could not carry on. The message says why."""


class NeedsHuman(Stopped):
    """The site asked for a human check."""


def clean(text):
    """'ab12 cde' -> 'AB12CDE'"""
    return re.sub(r'[^A-Z0-9]', '', (text or '').upper())


def appears(locator, ms):
    """True if it shows up within ms."""
    try:
        locator.wait_for(timeout=ms)
        return True
    except Exception:
        return False


def value_after(lines, label):
    """The value on the same line as a label, or on the line below it."""
    for i, line in enumerate(lines):
        if line.lower().startswith(label.lower()):
            same_line = line[len(label):].lstrip(' \t:')
            if same_line:
                return same_line
            return lines[i + 1] if i + 1 < len(lines) else ''
    return ''


def read_result(screen, reg):
    """The result screen as INSURED or NOT_INSURED, or Stopped if it's neither."""
    lines = [line.strip() for line in screen['text'].split('\n') if line.strip()]
    flat = ' '.join(lines)

    # Only the headline sentence counts. The small print on the same screen
    # contains the words "not showing as insured", and "NOT INSURED" contains
    # "INSURED", so searching the whole screen would go wrong both ways.
    headline = HEADLINE.search(flat)
    verdict = ' '.join(headline.group(1).upper().split()) if headline else ''
    if verdict == 'INSURED':
        status = 'INSURED'
    elif verdict in ('NOT INSURED', 'UNINSURED'):
        status = 'NOT_INSURED'
    else:
        raise Stopped(
            'the screen after the registration was not a result the check recognises')

    # The screen must be showing the plate that was asked about
    shows_plate = (
        any(clean(line) == reg or clean(line).endswith(reg) for line in lines)
        or any(clean(value) == reg for value in screen.get('fields') or []))
    if not shows_plate:
        raise Stopped('the result screen does not show the registration that was entered')

    site_time = SITE_TIME.search(flat)
    return {
        'status': status,
        'detail': verdict,
        'make_model': value_after(lines, 'Make and model'),
        'site_time': site_time.group(0) if site_time else '',
    }


def human_check_showing(page):
    """Only used to explain a check that has already stopped."""
    try:
        frames = page.locator(CHALLENGE_FRAMES)
        if any(frames.nth(i).is_visible() for i in range(frames.count())):
            return True
        return bool(CHALLENGE_TEXT.search(page.locator('body').inner_text(timeout=3000)))
    except Exception:
        return False


def dismiss_cookie_banner(page):
    """If a cookie banner is up, close it so it can't sit over the buttons."""
    try:
        buttons = page.get_by_role('button', name=COOKIE_BUTTON)
        for i in range(buttons.count()):
            if buttons.nth(i).is_visible():
                buttons.nth(i).click(timeout=5000)
                return
    except Exception:
        pass


def tick_terms_box(page):
    box = page.get_by_role('checkbox')
    agree = page.get_by_role('button', name='Agree and continue', exact=True)

    def can_agree(ms):
        # "Agree and continue" only becomes usable once the box is ticked,
        # so that button is the test of whether the tick worked
        try:
            agree.click(trial=True, timeout=ms)
            return True
        except Exception:
            return False

    try:
        box.click(timeout=5000)
    except Exception:
        pass
    try:
        ticked = box.is_checked(timeout=1000)
    except Exception:
        ticked = False
    if not ticked and not can_agree(2000):
        # Some designs hide the real box behind its label. Click the very start
        # of the label text, well clear of the "Terms & Conditions" link in it.
        try:
            page.get_by_text(
                re.compile(r'I confirm that I have read and accept', re.I)
            ).first.click(position={'x': 4, 'y': 6}, timeout=5000)
        except Exception:
            pass
    if not can_agree(5000):
        raise Stopped('could not tick the terms box')


def check(page, reg):
    """
    One check of one registration. Returns {'status': 'INSURED' or
    'NOT_INSURED', 'make_model', 'site_time', 'detail'}, or raises Stopped
    (NeedsHuman when the site wants a human check) with the reason.
    """
    step = 'opening the site'
    try:
        page.set_default_timeout(STEP_MS)
        answer = page.goto(START_URL, wait_until='domcontentloaded', timeout=STEP_MS)
        if answer and answer.status >= 400:
            raise Stopped(
                f'the site answered with error {answer.status} instead of its first screen')

        # Screen 1: choose "Personal check"
        step = 'choosing the personal check'
        if not appears(page.get_by_test_id('LandingPage'), STEP_MS):
            raise Stopped('the first screen did not appear')
        dismiss_cookie_banner(page)
        # Select it explicitly and confirm it took: the third-party card can be
        # the one that is pre-selected, and a click that lands before the page
        # has finished loading its scripts does nothing.
        chosen = page.locator('[data-testid="personal_check_card"][aria-checked="true"]')
        for _ in range(10):
            page.get_by_test_id('personal_check_card').click()
            if appears(chosen, 1000):
                break
        else:
            raise Stopped('could not select "Personal check"')
        if page.get_by_test_id('tp_check_card').get_attribute('aria-checked') != 'false':
            raise Stopped('"Third party check" still looks selected')
        page.get_by_test_id('continueBtn').click()

        # Screen 2: terms (skipped if the site goes straight to the registration)
        step = 'accepting the terms'
        terms = page.get_by_text('Use & Terms', exact=True)
        reg_screen = page.get_by_text('Enter your VRN', exact=True)
        terms.or_(reg_screen).first.wait_for()
        if terms.is_visible():
            tick_terms_box(page)
            page.get_by_role('button', name='Agree and continue', exact=True).click()

        # Screen 3: enter the registration
        step = 'entering the registration'
        reg_screen.wait_for()
        box = page.get_by_label(
            re.compile(r'Vehicle Registration Number', re.I)
        ).or_(page.get_by_role('textbox')).first
        box.fill(reg)
        if clean(box.input_value()) != reg:
            box.fill('')
            box.press_sequentially(reg, delay=60)
        if clean(box.input_value()) != reg:
            raise Stopped(
                'the registration box does not show the registration that was typed')
        page.get_by_role('button', name='Check Your Vehicle', exact=True).click()

        # Screen 4: read the result
        step = 'reading the result'
        try:
            page.wait_for_function(RESULT_OR_MOVED_ON, timeout=RESULT_MS)
        except Exception:
            pass
        # allow for a loading screen in between
        appears(page.get_by_text(re.compile(r'This vehicle is showing as', re.I)).first,
                STEP_MS)
        return read_result(page.evaluate(READ_SCREEN), reg)
    except Exception as exc:
        if human_check_showing(page):
            raise NeedsHuman(HUMAN_CHECK) from None
        if isinstance(exc, Stopped):
            why = str(exc)
        elif type(exc).__name__ == 'TimeoutError':
            why = 'the site did not show what the check was waiting for'
        else:
            why = str(exc).split('\n')[0]
        raise Stopped(f'while {step}: {why}') from None

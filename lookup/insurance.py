import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from importlib.util import find_spec

from django.utils import timezone

logger = logging.getLogger(__name__)

MIB_PAGE = 'https://enquiry.navigate.mib.org.uk/checkyourvehicle'
MIB_URL = os.environ.get('MIB_URL', MIB_PAGE)
HEADLESS = os.environ.get('MIB_HEADLESS', 'true').strip().lower() != 'false'

TURN_SECONDS = 20

NOTICES = {
    'limit': "MIB's search limit has been reached, try again later",
    'busy': 'another check is running, try again in a minute',
    'unknown': "couldn't check right now",
    'uninstalled': 'no browser installed on this server',
    'unavailable': "the browser wouldn't start on this server",
}
RETRY = ('busy', 'unknown')

_one_at_a_time = threading.Lock()
_memory = threading.Lock()
_answers = {}

_SUBMIT_ENABLED_JS = """() => {
    const button = document.querySelector('[data-testid="continueBtn"]');
    return button && !button.disabled;
}"""

_OUTCOME_JS = r"""() => {
    const panel = document.querySelector('[data-testid="VRNResultPage"]');
    if (panel && panel.getClientRects().length) return "result";
    const text = document.body.innerText.replace(/\s+/g, " ");
    if (text.includes("reached your limit of searches")) return "limit";
    return false;
}"""


class SearchLimitReached(Exception):
    pass


class BrowserUnavailable(Exception):
    pass


def available():
    return find_spec('playwright') is not None


def check(registration):
    if not available():
        return {'status': 'uninstalled'}
    answer = _remembered(registration)
    if answer:
        return answer
    if not _one_at_a_time.acquire(timeout=TURN_SECONDS):
        return {'status': 'busy'}
    try:
        answer = _remembered(registration)
        if answer:
            return answer
        return _remember(registration, lookup(registration))
    finally:
        _one_at_a_time.release()


def row(result):
    status = result['status']
    if status in ('insured', 'uninsured'):
        insured = status == 'insured'
        more = [f"MIB shows it as {'insured' if insured else 'not insured'} today"]
        if result.get('vehicle'):
            more.append(f"MIB lists it as {result['vehicle']}")
        if result.get('checked'):
            checked = timezone.localtime(result['checked'])
            more.append(f'Checked at {checked:%H:%M}')
        return {
            'status': status,
            'ok': insured,
            'label': 'Insured' if insured else 'Not insured',
            'more': more,
        }
    return {
        'status': status,
        'ok': None,
        'notice': f'({NOTICES.get(status, NOTICES["unknown"])})',
        'retry': status in RETRY,
    }


def lookup(registration):
    try:
        text = _read_result_page(registration)
    except SearchLimitReached:
        logger.warning('MIB search limit reached.')
        return {'status': 'limit'}
    except BrowserUnavailable as error:
        logger.exception('The browser for the MIB check could not start.')
        cause = error.__cause__
        missing = (isinstance(cause, FileNotFoundError)
                   or "Executable doesn't exist" in str(cause))
        return {'status': 'uninstalled' if missing else 'unavailable'}
    except Exception:
        logger.exception('MIB lookup failed.')
        return {'status': 'unknown'}
    result = _parse(text, registration)
    logger.info('MIB lookup finished: %s', result['status'])
    return result


def _remembered(registration):
    today = timezone.localdate()
    with _memory:
        kept = _answers.get(registration)
        if kept and kept['day'] == today:
            return kept['answer']
        _answers.pop(registration, None)
    return None


def _remember(registration, result):
    status = result['status']
    with _memory:
        if status in ('insured', 'uninsured'):
            now = timezone.now()
            today = timezone.localdate(now)
            result = dict(result, checked=now)
            for old in [reg for reg, kept in _answers.items()
                        if kept['day'] != today]:
                del _answers[old]
            _answers[registration] = {'day': today, 'answer': result}
    return result


def _open_display():
    if HEADLESS or not sys.platform.startswith('linux'):
        return
    display = os.environ.setdefault('DISPLAY', ':99')
    if not display.startswith(':'):
        return
    path = f"/tmp/.X11-unix/X{display[1:].partition('.')[0]}"
    if _display_is_up(path):
        return
    subprocess.Popen(
        ['Xvfb', display, '-screen', '0', '1280x800x24',
         '-nolisten', 'tcp', '-noreset'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if _display_is_up(path):
            break
        time.sleep(0.1)


def _display_is_up(path):
    for address in (path, '\0' + path):
        try:
            with socket.socket(socket.AF_UNIX) as probe:
                probe.connect(address)
            return True
        except OSError:
            pass
    return False


def _read_result_page(registration):
    from playwright.sync_api import sync_playwright

    try:
        _open_display()
    except Exception as error:
        raise BrowserUnavailable from error
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=HEADLESS)
        except Exception as error:
            raise BrowserUnavailable from error
        try:
            page = browser.new_page()
            page.set_default_timeout(15_000)

            failed = []

            def note_failure(response):
                if response.status >= 400:
                    failed.append(f'{response.status} {response.url}')

            page.on('response', note_failure)

            try:
                return _click_through(page, registration)
            except SearchLimitReached:
                raise
            except Exception:
                _log_what_mib_showed(page, failed)
                raise
        finally:
            browser.close()


def _click_through(page, registration):
    page.goto(MIB_URL, wait_until='domcontentloaded')
    card = page.get_by_test_id('personal_check_card')
    card.wait_for(state='visible')
    card.click()
    page.get_by_test_id('continueBtn').click()

    page.get_by_role('heading', name='Use & Terms').wait_for(state='visible')
    checkbox = page.get_by_role('checkbox')
    if checkbox.get_attribute('aria-checked') != 'true':
        checkbox.click()
    page.get_by_role('button', name='Agree and continue').click()

    vrm = page.get_by_test_id('vrm_searchtext')
    vrm.wait_for(state='visible')
    vrm.fill(registration)
    vrm.press('Tab')

    banner = page.get_by_test_id('userCookiesBanner')
    if banner.count() and banner.is_visible():
        reject = page.get_by_role('button', name='Reject analytics cookies')
        if reject.count() and reject.is_visible():
            reject.click()

    page.wait_for_function(_SUBMIT_ENABLED_JS)
    page.get_by_test_id('continueBtn').click()

    outcome = page.wait_for_function(
        _OUTCOME_JS, timeout=25_000, polling=250
    ).json_value()
    if outcome == 'limit':
        raise SearchLimitReached
    return page.get_by_test_id('VRNResultPage').inner_text()


def _log_what_mib_showed(page, failed):
    try:
        text = ' '.join(page.locator('body').inner_text(timeout=3_000).split())
        logger.warning(
            'MIB page at failure: url=%s | title=%s | failed requests=%s | '
            'text=%s',
            page.url,
            page.title(),
            failed[-10:],
            text[:1500],
        )
    except Exception:
        logger.warning('MIB page at failure could not be read.')


def _parse(text, registration):
    shown = re.search(
        r'Vehicle Registration Number:\s*([A-Z0-9 ]+)', text, re.IGNORECASE
    )
    if not shown:
        return {'status': 'unknown'}
    if re.sub(r'[^A-Z0-9]', '', shown.group(1).upper()) != registration:
        return {'status': 'unknown'}

    status = re.search(
        r'This vehicle is showing as\s+(NOT\s+INSURED|UNINSURED|INSURED)\s+'
        r'in Navigate today',
        text,
        re.IGNORECASE,
    )
    if not status:
        return {'status': 'unknown'}
    if status.group(1).upper() != 'INSURED':
        return {'status': 'uninsured'}

    vehicle = re.search(r'Make and model:\s*([^\n]+)', text, re.IGNORECASE)
    return {
        'status': 'insured',
        'vehicle': vehicle.group(1).strip() if vehicle else '',
    }

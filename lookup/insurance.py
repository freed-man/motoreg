"""
On-demand insurance check for your own cars, using tools/mid_check.py.

The check runs in a background thread with a headless Chrome, because it
takes longer than Heroku allows a web request to run; the result page asks
for progress every couple of seconds. If askMID wants a human check, the
script stops and the page says so: nothing here tries to get past it.
"""

import importlib.util
import logging
import shutil
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

SCRIPT = Path(settings.BASE_DIR) / 'tools' / 'mid_check.py'
SCREENSHOTS = Path(tempfile.gettempdir()) / 'mid_results'
HEROKU_CHROME = '/app/.chrome-for-testing/chrome-linux64/chrome'
LOCK_KEY = 'insurance-lock'
RUNNING_FOR = 3 * 60    # give up on a check that hasn't finished in 3 minutes
RESULT_FOR = 15 * 60    # keep an answer 15 minutes before allowing a re-check
FAILED_FOR = 10 * 60    # keep a failed check's explanation for 10 minutes
RESULT_TIMEOUT = 45     # seconds the script waits for askMID's answer
ANSWERS = ('INSURED', 'NOT_INSURED')


class NeedsHuman(Exception):
    """askMID asked for a human check, or the script couldn't find its way."""


def _key(reg):
    return f'insurance:{reg}'


def status(reg):
    """None, or {'state': 'running'} or {'state': 'done', 'status': ...}."""
    return cache.get(_key(reg))


def start(reg):
    """Start a check unless one is running or a recent answer exists."""
    current = status(reg)
    if current and (current['state'] == 'running' or current.get('status') in ANSWERS):
        return current    # a failed check can be retried straight away
    if not cache.add(LOCK_KEY, reg, RUNNING_FOR):
        return {'state': 'busy'}    # another of your cars is being checked
    running = {'state': 'running'}
    cache.set(_key(reg), running, RUNNING_FOR)
    threading.Thread(target=_job, args=(reg,), daemon=True).start()
    return running


def _job(reg):
    try:
        result = run_check(reg)
    except Exception as exc:    # never leave a check stuck on "running"
        logger.exception('Insurance check failed for %s', reg)
        result = {'status': 'ERROR', 'detail': f'{type(exc).__name__}: {exc}'}
    done = {
        'state': 'done',
        'status': result.get('status', 'ERROR'),
        'make_model': result.get('make_model', ''),
        'site_time': result.get('site_time', ''),
        'detail': result.get('detail', ''),
    }
    if done['status'] == 'ERROR' and done['detail'].startswith('NeedsHuman'):
        if 'human check' in done['detail']:
            done['status'] = 'HUMAN_CHECK'
    answered = done['status'] in ANSWERS
    if not answered:
        done['reason'] = _reason(done['detail'])
        done['seen'] = result.get('seen', {})
    cache.set(_key(reg), done, RESULT_FOR if answered else FAILED_FOR)
    cache.delete(LOCK_KEY)


def _reason(detail):
    """The short why, from mid_check's message or the error."""
    message = detail.split(': ', 1)[-1]
    if 'human check' in message and '(' in message:
        return message[message.find('(') + 1:message.rfind(')')]
    return message.split('. ')[0][:160]


def describe_page(page):
    """What the browser was looking at when the check stopped."""
    try:
        frames = [f.url[:160] for f in page.frames
                  if f != page.main_frame and f.url and f.url != 'about:blank']
        text = ' '.join(page.locator('body').inner_text(timeout=3000).split())
        return {'title': page.title()[:160], 'address': page.url[:160],
                'frames': frames[:8], 'text': text[:500]}
    except Exception as exc:
        return {'error': f'{type(exc).__name__}: {exc}'[:200]}


def load_script():
    """tools/mid_check.py, fresh each time so an updated script is picked up."""
    spec = importlib.util.spec_from_file_location('mid_check', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def launch_browser(playwright):
    """Heroku's Chrome for Testing when it's there, otherwise a local one."""
    chrome = shutil.which('chrome') or (
        HEROKU_CHROME if Path(HEROKU_CHROME).exists() else None)
    if chrome:
        return playwright.chromium.launch(executable_path=chrome, headless=True)
    try:
        return playwright.chromium.launch(headless=True)
    except Exception:    # no Playwright Chromium downloaded: use installed Chrome
        return playwright.chromium.launch(channel='chrome', headless=True)


@contextmanager
def browser_page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        try:
            yield browser.new_page(viewport={'width': 1200, 'height': 900})
        finally:
            browser.close()


def _stop(message):
    raise NeedsHuman(message)


def run_check(reg):
    """One askMID check, headless. Returns mid_check's result dict."""
    try:
        mid = load_script()
    except SystemExit as exc:    # the script exits if Playwright isn't installed
        raise RuntimeError(str(exc)) from None
    mid.pause = _stop    # on the server nobody can step in, so stop instead
    with browser_page() as page:
        result = mid.check_one(page, reg, mid.DEFAULT_URL, SCREENSHOTS, RESULT_TIMEOUT)
        if result.get('status') not in ANSWERS:
            result['seen'] = describe_page(page)
        return result

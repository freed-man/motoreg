"""
Calls to the DVLA Vehicle Enquiry Service and the DVSA MOT History API.

Both are asked at the same time, and the DVSA OAuth token is cached until
shortly before it expires instead of being fetched on every lookup.
"""

import os
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

import requests
from django.core.cache import cache

TIMEOUT = 10  # seconds, per request
TOKEN_CACHE_KEY = 'dvsa-mot-token'
MOT_SETTINGS = (
    'MOT_TOKEN_URL', 'MOT_CLIENT_ID', 'MOT_CLIENT_SECRET',
    'MOT_SCOPE', 'MOT_API_BASE', 'MOT_API_KEY',
)

# status is one of: ok, not_found, auth, busy, not_configured, error
Result = namedtuple('Result', 'status data')


def fetch_vehicle(registration):
    """Look a reg up at DVLA and DVSA in parallel. Returns (dvla, mot)."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        dvla = pool.submit(fetch_dvla, registration)
        mot = pool.submit(fetch_mot, registration)
        return dvla.result(), mot.result()


def _status_for(status_code):
    if status_code == 200:
        return 'ok'
    if status_code in (400, 404):
        return 'not_found'
    if status_code in (401, 403):
        return 'auth'
    if status_code == 429:
        return 'busy'
    return 'error'


def _result_from(response):
    status = _status_for(response.status_code)
    if status != 'ok':
        return Result(status, None)
    try:
        return Result('ok', response.json())
    except ValueError:
        return Result('error', None)


def fetch_dvla(registration):
    """Tax, MOT status, colour, CO2, V5C date and so on."""
    url = os.environ.get('DVLA_API_URL')
    api_key = os.environ.get('DVLA_API_KEY')
    if not url or not api_key:
        return Result('not_configured', None)
    try:
        response = requests.post(
            url,
            json={'registrationNumber': registration},
            headers={'x-api-key': api_key},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return Result('error', None)
    return _result_from(response)


def _mot_token(fresh=False):
    """OAuth2 client credentials token for the MOT History API."""
    if not fresh:
        token = cache.get(TOKEN_CACHE_KEY)
        if token:
            return token
    try:
        response = requests.post(
            os.environ['MOT_TOKEN_URL'],
            data={
                'grant_type': 'client_credentials',
                'client_id': os.environ['MOT_CLIENT_ID'],
                'client_secret': os.environ['MOT_CLIENT_SECRET'],
                'scope': os.environ['MOT_SCOPE'],
            },
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            return None
        body = response.json()
    except (requests.RequestException, ValueError):
        return None

    token = body.get('access_token')
    if token:
        try:
            lifetime = int(body.get('expires_in', 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        cache.set(TOKEN_CACHE_KEY, token, max(lifetime - 120, 60))
    return token


def fetch_mot(registration):
    """Model, first registration date and every MOT test."""
    if not all(os.environ.get(name) for name in MOT_SETTINGS):
        return Result('not_configured', None)

    base = os.environ['MOT_API_BASE'].rstrip('/')
    url = f'{base}/v1/trade/vehicles/registration/{registration}'

    for attempt in range(2):
        token = _mot_token(fresh=attempt > 0)
        if not token:
            return Result('error', None)
        try:
            response = requests.get(
                url,
                headers={
                    'Authorization': f'Bearer {token}',
                    'X-API-Key': os.environ['MOT_API_KEY'],
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            return Result('error', None)
        if response.status_code != 401:
            break
        # 401 on the first try means the cached token went stale early:
        # loop once more with a fresh one

    return _result_from(response)

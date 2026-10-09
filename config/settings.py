"""
Settings for the personal reg lookup site.

Secrets come from environment variables: env.py locally (gitignored),
config vars on Heroku. The DVLA_ and MOT_ names are the same ones the
old auto:commit site used, so its values can be copied straight across.
"""

import os
import tempfile
from pathlib import Path

if os.path.isfile('env.py'):
    import env  # noqa: F401

BASE_DIR = Path(__file__).resolve().parent.parent

# Shown in the navbar and page titles. This is the only place the name lives.
SITE_NAME = 'motoreg'

# askMID's free insurance check. It turns automated browsers away, so the site
# can't run the check for you: its buttons copy the reg and open this page.
ASKMID_URL = 'https://checkyourvehicle.org.uk/checkyourvehicle'

SECRET_KEY = os.environ.get('SECRET_KEY')

DEBUG = os.environ.get('DEVELOPMENT', '') == 'True'

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '.herokuapp.com', '.up.railway.app']
# Extra hosts such as a custom domain, comma separated
ALLOWED_HOSTS += [
    host.strip() for host in os.environ.get('EXTRA_HOSTS', '').split(',')
    if host.strip()
]

# Optional. When set, the site asks for this password once a day in each browser.
SITE_PASSWORD = os.environ.get('SITE_PASSWORD', '')

INSTALLED_APPS = [
    'django.contrib.staticfiles',
    'lookup',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'lookup.middleware.SitePasswordMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'lookup.context_processors.site',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# No database: lookups are never stored. The session (only used to remember
# the SITE_PASSWORD unlock) lives in a signed cookie instead.
DATABASES = {}
SESSION_ENGINE = 'django.contrib.sessions.backends.signed_cookies'

# File based, so every web worker shares one DVSA token
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': os.path.join(tempfile.gettempdir(), 'motoreg-cache'),
    },
}
SESSION_COOKIE_AGE = 60 * 60 * 24  # ask for the password again after a day
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Heroku and Railway terminate HTTPS and pass the original scheme on
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

LANGUAGE_CODE = 'en-gb'
TIME_ZONE = 'Europe/London'  # so "days remaining" flips at UK midnight
USE_I18N = False
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Serve from static/ directly, so the pages work without collectstatic
WHITENOISE_USE_FINDERS = True

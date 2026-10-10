import os
import tempfile
from pathlib import Path

if os.path.isfile('env.py'):
    import env  # noqa: F401

BASE_DIR = Path(__file__).resolve().parent.parent

SITE_NAME = 'motoreg'

SECRET_KEY = os.environ.get('SECRET_KEY')

DEBUG = os.environ.get('DEVELOPMENT', '') == 'True'

ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '.herokuapp.com', '.up.railway.app']
ALLOWED_HOSTS += [
    host.strip() for host in os.environ.get('EXTRA_HOSTS', '').split(',')
    if host.strip()
]

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

DATABASES = {}
SESSION_ENGINE = 'django.contrib.sessions.backends.signed_cookies'

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.filebased.FileBasedCache',
        'LOCATION': os.path.join(tempfile.gettempdir(), 'motoreg-cache'),
    },
}
SESSION_COOKIE_AGE = 60 * 60 * 24
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

LANGUAGE_CODE = 'en-gb'
TIME_ZONE = 'Europe/London'
USE_I18N = False
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
WHITENOISE_USE_FINDERS = True

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {'lookup': {'handlers': ['console'], 'level': 'INFO'}},
}

"""
Optional lock for the whole site. Does nothing unless the SITE_PASSWORD
config var is set; then every page asks for it, once a day in each browser.
"""

from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.crypto import constant_time_compare, salted_hmac


def unlock_token():
    """Changes whenever SITE_PASSWORD changes, so old cookies stop working."""
    return salted_hmac('site-password', settings.SITE_PASSWORD).hexdigest()


class SitePasswordMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        unlock_url = reverse('unlock')
        if (
            not settings.SITE_PASSWORD
            or request.path == unlock_url
            or request.path.startswith(settings.STATIC_URL)
            or constant_time_compare(
                request.session.get('unlock', ''), unlock_token())
        ):
            return self.get_response(request)
        query = urlencode({'next': request.get_full_path()})
        return redirect(f'{unlock_url}?{query}')

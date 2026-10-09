from django.conf import settings


def site(request):
    """Make SITE_NAME and ASKMID_URL available in every template."""
    return {
        'SITE_NAME': settings.SITE_NAME,
        'ASKMID_URL': settings.ASKMID_URL,
    }

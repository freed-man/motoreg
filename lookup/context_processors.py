from django.conf import settings


def site(request):
    """Make SITE_NAME available in every template."""
    return {'SITE_NAME': settings.SITE_NAME}

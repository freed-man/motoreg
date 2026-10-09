import re

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.utils.http import url_has_allowed_host_and_scheme

from .details import build_details
from .middleware import unlock_token
from .services import fetch_vehicle

# UK regs are 2 to 7 letters and numbers, always with at least one number
REG_PATTERN = re.compile(r'(?=.*\d)[A-Z0-9]{2,7}')

DVLA_ERRORS = {
    'not_found': (
        'No vehicle found for {reg}. Check the registration and try again.'
    ),
    'auth': 'DVLA rejected the API key. Check DVLA_API_KEY.',
    'busy': 'DVLA rate limit reached. Try again in a minute.',
    'not_configured': 'DVLA_API_KEY or DVLA_API_URL is missing.',
    'error': "Couldn't reach the DVLA lookup service. Try again in a minute.",
}


def clean_reg(value):
    """'ab12 cde' -> 'AB12CDE'"""
    return re.sub(r'[^A-Z0-9]', '', (value or '').upper())


def home(request):
    return render(request, 'lookup/index.html')


def lookup(request):
    """The home page form: tidy the reg up and go to its page."""
    reg = clean_reg(request.GET.get('reg'))
    if not REG_PATTERN.fullmatch(reg):
        return render(request, 'lookup/index.html', {
            'error': (
                "That doesn't look like a UK registration. "
                'Use 2 to 7 letters and numbers.'
            ),
            'reg': reg[:7],
        }, status=400)
    return redirect('result', reg=reg)


def result(request, reg):
    """/AB12CDE: look the vehicle up and show everything."""
    clean = clean_reg(reg)
    if not re.fullmatch(r'[A-Za-z0-9 ]+', reg) or not REG_PATTERN.fullmatch(
            clean):
        raise Http404
    canonical = reverse('result', args=[clean])
    if request.path != canonical:
        return redirect(canonical)

    dvla, mot, lez = fetch_vehicle(clean)
    if dvla.status != 'ok':
        return render(request, 'lookup/index.html', {
            'error': DVLA_ERRORS[dvla.status].format(reg=clean),
            'reg': clean,
        }, status=404 if dvla.status == 'not_found' else 503)

    context = build_details(dvla.data, mot.data, mot_status=mot.status,
                            lez=lez.data, lez_status=lez.status)
    return render(request, 'lookup/result.html', context)


def insurance_page(request):
    """/insurance/: type a reg, and the button copies it and opens askMID."""
    return render(request, 'lookup/insurance.html', {
        'reg': clean_reg(request.GET.get('reg'))[:7],
    })


def unlock(request):
    """Password page, only used when SITE_PASSWORD is set."""
    next_url = request.POST.get('next') or request.GET.get('next') or '/'
    if not url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()},
            require_https=request.is_secure()):
        next_url = '/'
    if not settings.SITE_PASSWORD:
        return redirect(next_url)

    error = ''
    if request.method == 'POST':
        if constant_time_compare(
                request.POST.get('password', ''), settings.SITE_PASSWORD):
            request.session['unlock'] = unlock_token()
            return redirect(next_url)
        error = 'Wrong password. Try again.'
    return render(request, 'lookup/unlock.html', {
        'next': next_url, 'error': error,
    })

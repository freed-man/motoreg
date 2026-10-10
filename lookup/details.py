import json

from django.utils import timezone

from . import checks

MAKE_LOGO_MAP = {
    'mercedes': 'mercedes_benz',
    'vw': 'volkswagen',
    'landrover': 'land_rover',
    'alfaromeo': 'alfa_romeo',
}

NO_MOT_HISTORY = 'No MOT history available for this vehicle.'

MOT_PROBLEMS = {
    'not_configured': (
        'MOT history is off because the MOT_ config vars are missing.'
    ),
    'auth': (
        'DVSA rejected the MOT API credentials, so MOT history is missing. '
        'Check the MOT_ config vars.'
    ),
    'busy': (
        'DVSA rate limit reached, so MOT history is missing. '
        'Refresh to try again.'
    ),
    'error': (
        "Couldn't load MOT history from DVSA. Refresh to try again."
    ),
}


def mot_record(mot):
    if isinstance(mot, list):
        mot = mot[0] if mot else None
    return mot if isinstance(mot, dict) else {}


def sentence_case(text):
    return (text or '').strip().capitalize()


def build_details(dvla, mot, mot_status='ok', lez=None, lez_status='',
                  today=None):
    today = today or timezone.localdate()
    record = mot_record(mot)
    letter = lez.get('s', '') if isinstance(lez, dict) else ''

    make = dvla.get('make') or ''
    logo = make.lower().replace('-', '_').replace(' ', '_')
    fuel = dvla.get('fuelType') or ''
    if fuel.upper() == 'ELECTRICITY':
        fuel = 'Electric'
    engine = dvla.get('engineCapacity')
    mot_problem = MOT_PROBLEMS.get(mot_status, '')

    details = {
        'registration': dvla.get('registrationNumber') or '',
        'make': make,
        'model': record.get('model') or '',
        'make_logo': MAKE_LOGO_MAP.get(logo, logo),
        'colour': sentence_case(dvla.get('colour')),
        'fuel': sentence_case(fuel),
        'engine': f'{engine}cc' if engine else '',
        'mot_problem': bool(mot_problem),
        'raw_json': json.dumps(
            {'dvla': dvla, 'dvsa_mot': mot, 'lez_scotland': lez}, indent=2),
    }
    details.update(checks.display(
        checks.facts(dvla, record, (lez_status, letter)), today))
    details['mot_notice'] = mot_problem or (
        '' if details.get('vc_mot_tests') else NO_MOT_HISTORY)
    return details

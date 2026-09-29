"""
Turns the raw DVLA and DVSA responses into what the result page shows.

Ported from auto:commit's vehicles/views.py (vehicle_detail). The output
is the same, apart from "1 day" instead of "1 days", and the repeated
age and countdown maths now living in two small helpers.
"""

import copy
import json
from datetime import date, datetime

from django.utils import timezone

from .tax_rates import get_annual_tax

MAKE_LOGO_MAP = {
    'mercedes': 'mercedes_benz',
    'vw': 'volkswagen',
    'landrover': 'land_rover',
    'alfaromeo': 'alfa_romeo',
}

MAJOR_DEFECT_TYPES = ('DANGEROUS', 'MAJOR', 'FAIL', 'PRS')

NO_MOT_HISTORY = 'No MOT history available for this vehicle.'

# Shown instead of the MOT list when the DVSA call didn't work
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


def format_date(date_string):
    """Convert API date strings to dd/mm/yyyy format."""
    if not date_string:
        return None
    try:
        if 'T' in date_string:
            dt = datetime.strptime(date_string[:10], '%Y-%m-%d')
        else:
            dt = datetime.strptime(date_string, '%Y-%m-%d')
        return dt.strftime('%d/%m/%Y')
    except ValueError:
        return date_string


def format_mileage(value):
    """Format mileage with commas e.g. 103449 -> 103,449"""
    if not value:
        return None
    try:
        return f'{int(value):,}'
    except (ValueError, TypeError):
        return value


def extract_mot_field(mot_data, field_name):
    """Extract a field from MOT data which can be dict or list."""
    if mot_data and isinstance(mot_data, dict):
        return mot_data.get(field_name)
    elif mot_data and isinstance(mot_data, list) and len(mot_data) > 0:
        return mot_data[0].get(field_name)
    return None


def parse_date(value):
    """'2024-05-01' or '2024-05-01T10:00:00.000Z' -> date, else None."""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def plural(count, word):
    return f"{count} {word}{'s' if count != 1 else ''}"


def span_text(start, today):
    """Time since start, e.g. '3 years, 2 months'."""
    delta = (today - start).days
    years = delta // 365
    months = (delta % 365) // 30
    if years > 0 and months > 0:
        return f"{plural(years, 'year')}, {plural(months, 'month')}"
    if years > 0:
        return plural(years, 'year')
    return plural(months, 'month')


def countdown_text(target, today, today_text):
    """'12 days remaining', today_text on the day, or '3 days overdue'."""
    delta = (target - today).days
    if delta > 0:
        return f"{plural(delta, 'day')} remaining"
    if delta == 0:
        return today_text
    return f"{plural(abs(delta), 'day')} overdue"


def build_details(dvla, mot, mot_status='ok', today=None):
    """Everything result.html needs, from the two API responses."""
    today = today or timezone.localdate()
    raw_json = json.dumps({'dvla': dvla, 'dvsa_mot': mot}, indent=2)
    dvla = copy.deepcopy(dvla)
    mot = copy.deepcopy(mot)

    # model and mot tests come from DVSA, newest test first
    model = extract_mot_field(mot, 'model')
    mot_tests = extract_mot_field(mot, 'motTests') or []
    mot_tests.sort(key=lambda t: t.get('completedDate') or '', reverse=True)

    # make logo filename
    make_raw = (dvla.get('make') or '').lower().replace(
        '-', '_').replace(' ', '_')
    make_logo = MAKE_LOGO_MAP.get(make_raw, make_raw)

    # clean up fuel type for display (ELECTRICITY -> ELECTRIC)
    fuel_display = dvla.get('fuelType', '')
    if fuel_display.upper() == 'ELECTRICITY':
        fuel_display = 'ELECTRIC'

    # vehicle age from the DVSA registration date, else the DVLA year
    vehicle_age_text = ''
    started = parse_date(extract_mot_field(mot, 'registrationDate'))
    if not started and dvla.get('yearOfManufacture'):
        try:
            started = date(int(dvla['yearOfManufacture']), 1, 1)
        except (ValueError, TypeError):
            started = None
    if started:
        vehicle_age_text = f'{span_text(started, today)} old'

    # mot days remaining/overdue
    mot_expiry = parse_date(dvla.get('motExpiryDate'))
    mot_days_text = (
        countdown_text(mot_expiry, today, 'expires today') if mot_expiry
        else ''
    )

    # for new cars with no MOT, use DVSA motTestDueDate
    new_car_mot = False
    new_car_mot_date = ''
    new_car_mot_days = ''
    if dvla.get('motStatus') == 'No details held by DVLA':
        mot_due = parse_date(extract_mot_field(mot, 'motTestDueDate'))
        if mot_due:
            new_car_mot = True
            new_car_mot_date = mot_due.strftime('%d/%m/%Y')
            new_car_mot_days = countdown_text(mot_due, today, 'due today')

    # tax days remaining/overdue
    tax_due = parse_date(dvla.get('taxDueDate'))
    tax_days_text = (
        countdown_text(tax_due, today, 'due today') if tax_due else ''
    )

    # how long the current keeper has had the car
    v5c_issued = parse_date(dvla.get('dateOfLastV5CIssued'))
    v5c_days_text = span_text(v5c_issued, today) if v5c_issued else ''

    # estimated tax rate
    reg_month_raw = dvla.get('monthOfFirstRegistration', '')
    reg_year = None
    reg_month = None
    if reg_month_raw:
        try:
            parts = reg_month_raw.split('-')
            reg_year = int(parts[0])
            reg_month = int(parts[1])
        except (ValueError, IndexError):
            reg_year = dvla.get('yearOfManufacture')
    else:
        reg_year = dvla.get('yearOfManufacture')

    tax_estimate = get_annual_tax(
        dvla.get('co2Emissions'), dvla.get('fuelType', ''), reg_year,
        reg_month, dvla.get('engineCapacity')
    )

    # format dates for display
    if dvla.get('motExpiryDate'):
        dvla['motExpiryDateFormatted'] = format_date(dvla['motExpiryDate'])
    if dvla.get('taxDueDate'):
        dvla['taxDueDateFormatted'] = format_date(dvla['taxDueDate'])
    if dvla.get('dateOfLastV5CIssued'):
        dvla['v5cDateFormatted'] = format_date(dvla['dateOfLastV5CIssued'])

    # format mot test dates and mileage, group defects
    for test in mot_tests:
        test['completedDateFormatted'] = format_date(
            test.get('completedDate', '')
        )
        if test.get('expiryDate'):
            test['expiryDateFormatted'] = format_date(test['expiryDate'])
        if test.get('odometerValue'):
            test['mileageFormatted'] = format_mileage(test['odometerValue'])
        if test.get('odometerUnit'):
            if test['odometerUnit'].upper() == 'MI':
                test['odometerUnit'] = 'miles'

        if test.get('defects'):
            test['majors'] = [
                d for d in test['defects']
                if d.get('type') in MAJOR_DEFECT_TYPES
            ]
            test['advisories'] = [
                d for d in test['defects']
                if d.get('type') not in MAJOR_DEFECT_TYPES
            ]
            for defect in test['majors'] + test['advisories']:
                if defect.get('text'):
                    defect['text'] = defect['text'][0].upper() + defect['text'][1:]

    # mileage differences between tests (newest first, so compare with
    # the next, older, test)
    for i in range(len(mot_tests) - 1):
        current, previous = mot_tests[i], mot_tests[i + 1]
        if current.get('odometerValue') and previous.get('odometerValue'):
            try:
                diff = int(current['odometerValue']) - int(
                    previous['odometerValue'])
                current['mileage_diff'] = diff
                current['mileage_diff_formatted'] = f'{abs(diff):,}'
            except (ValueError, TypeError):
                pass

    # what to show when there's no MOT list
    mot_problem = MOT_PROBLEMS.get(mot_status, '')
    mot_notice = mot_problem or ('' if mot_tests else NO_MOT_HISTORY)

    return {
        'dvla': dvla,
        'model': model,
        'make_logo': make_logo,
        'fuel_display': fuel_display,
        'vehicle_age_text': vehicle_age_text,
        'mot_tests': mot_tests,
        'mot_days_text': mot_days_text,
        'tax_days_text': tax_days_text,
        'v5c_days_text': v5c_days_text,
        'new_car_mot': new_car_mot,
        'new_car_mot_date': new_car_mot_date,
        'new_car_mot_days': new_car_mot_days,
        'tax_estimate': tax_estimate,
        'mot_notice': mot_notice,
        'mot_problem': bool(mot_problem),
        'raw_json': raw_json,
    }

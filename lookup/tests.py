import copy
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, Mock, patch

import requests
from django.core.cache import cache
from django.test import Client, SimpleTestCase, override_settings

from . import checks, insurance
from .details import NO_MOT_HISTORY, build_details
from .services import Result, fetch_dvla, fetch_lez, fetch_mot, fetch_vehicle
from .tax_rates import get_annual_tax
from .views import REG_PATTERN, clean_reg

TODAY = date(2026, 9, 29)

DVLA = {
    'registrationNumber': 'AB12CDE',
    'taxStatus': 'Taxed',
    'taxDueDate': '2027-03-01',
    'motStatus': 'Valid',
    'motExpiryDate': '2027-01-15',
    'make': 'VOLKSWAGEN',
    'monthOfFirstRegistration': '2015-03',
    'yearOfManufacture': 2015,
    'engineCapacity': 1968,
    'co2Emissions': 119,
    'fuelType': 'DIESEL',
    'markedForExport': False,
    'colour': 'BLUE',
    'typeApproval': 'M1',
    'dateOfLastV5CIssued': '2021-06-10',
    'wheelplan': '2 AXLE RIGID BODY',
}

MOT = {
    'registration': 'AB12CDE',
    'make': 'VOLKSWAGEN',
    'model': 'GOLF',
    'registrationDate': '2015-03-20',
    'motTests': [
        {
            'completedDate': '2024-01-10T10:00:00.000Z',
            'testResult': 'PASSED',
            'expiryDate': '2025-01-15',
            'odometerValue': '61000',
            'odometerUnit': 'MI',
            'defects': [
                {'text': 'tyre worn close to legal limit', 'type': 'ADVISORY'},
            ],
        },
        {
            'completedDate': '2025-01-12T10:00:00.000Z',
            'testResult': 'FAILED',
            'odometerValue': '58000',
            'odometerUnit': 'MI',
            'defects': [{'text': 'brake pipe corroded', 'type': 'MAJOR'}],
        },
        {
            'completedDate': '2023-01-09T10:00:00.000Z',
            'testResult': 'PASSED',
            'expiryDate': '2024-01-15',
            'odometerValue': '50000',
            'odometerUnit': 'MI',
            'defects': [],
        },
    ],
}

LEZ_OK = {
    's': 'c',
    'vrn': 'AB12CDE',
    'make': 'VOLKSWAGEN',
    'vehicleType': 'CAR',
    'fuelType': 'DIESEL',
    'dateOfFirstRegistration': '2015-03-20',
}

API_ENV = {
    'DVLA_API_URL': 'https://dvla.example/vehicles',
    'DVLA_API_KEY': 'dvla-key',
    'MOT_TOKEN_URL': 'https://login.example/token',
    'MOT_CLIENT_ID': 'client',
    'MOT_CLIENT_SECRET': 'secret',
    'MOT_SCOPE': 'scope',
    'MOT_API_BASE': 'https://mot.example/',
    'MOT_API_KEY': 'mot-key',
}


def fake_response(status_code, body=None):
    response = Mock(status_code=status_code)
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    return response


class RegTests(SimpleTestCase):
    def test_clean_reg(self):
        self.assertEqual(clean_reg(' ab12 cde '), 'AB12CDE')
        self.assertEqual(clean_reg('ab-12.cde'), 'AB12CDE')
        self.assertEqual(clean_reg(None), '')

    def test_reg_pattern(self):
        for reg in ('AB12CDE', 'A1', '1ABC', 'ABZ1234'):
            self.assertTrue(REG_PATTERN.fullmatch(reg), reg)
        for reg in ('ABCDEFG', 'A', 'AB12CDEF', ''):
            self.assertFalse(REG_PATTERN.fullmatch(reg), reg)


class HomeAndFormTests(SimpleTestCase):
    def test_home_page(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lookup/index.html')
        self.assertContains(response, 'action="/lookup/"')
        self.assertContains(response, 'name="reg"')
        self.assertNotContains(response, 'Basket')
        self.assertNotContains(response, 'Login')
        self.assertNotContains(response, 'class="lead')
        self.assertNotContains(response, 'images/logo.png')

    def test_form_goes_to_clean_reg_page(self):
        response = self.client.get('/lookup/', {'reg': 'ab12 cde'})
        self.assertRedirects(
            response, '/AB12CDE', fetch_redirect_response=False)

    def test_form_rejects_nonsense(self):
        response = self.client.get('/lookup/', {'reg': 'hello'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(
            response, 'look like a UK registration', status_code=400)
        self.assertContains(response, 'value="HELLO"', status_code=400)

    def test_form_with_nothing(self):
        response = self.client.get('/lookup/')
        self.assertEqual(response.status_code, 400)


@patch('lookup.views.insurance.available', return_value=True)
@patch('lookup.views.fetch_vehicle')
class ResultPageTests(SimpleTestCase):
    def setUp(self):
        today = patch('lookup.details.timezone.localdate', return_value=TODAY)
        today.start()
        self.addCleanup(today.stop)

    def test_non_canonical_urls_redirect(self, fetch, available):
        for path in ('/ab12cde', '/AB12CDE/', '/AB12%20CDE', '/ab12 cde/'):
            response = self.client.get(path)
            self.assertRedirects(
                response, '/AB12CDE', fetch_redirect_response=False)
        fetch.assert_not_called()

    def test_junk_paths_404(self, fetch, available):
        for path in ('/admin', '/robots.txt', '/wp-login.php',
                     '/ABCDEFGH12', '/AB1.2'):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        fetch.assert_not_called()

    def test_full_result(self, fetch, available):
        fetch.return_value = (Result('ok', DVLA), Result('ok', MOT), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        fetch.assert_called_once_with('AB12CDE')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lookup/result.html')
        self.assertContains(response, '<title>AB12CDE | motoreg</title>')
        self.assertContains(response, '2015 VOLKSWAGEN GOLF')
        self.assertContains(response, 'images/makes/volkswagen.png')
        self.assertContains(response, '<span>2015</span><span class="vehicle-sub">(11 years, 6 months old)</span>')
        self.assertContains(response, '<span>10/06/2021</span><span class="vehicle-sub">(5 years, 3 months ago)</span>')
        self.assertContains(response, 'Expires 15/01/2027, 108 days remaining')
        self.assertContains(response, 'Expires 01/03/2027, 153 days remaining')
        self.assertContains(response, '<th scope="row">Standard</th><td>£35</td>')
        self.assertContains(response, '<span class="text-success">Compliant</span>')
        self.assertContains(response, 'MOT History (3)')
        self.assertContains(response, 'Mileage at each MOT')
        self.assertContains(response, 'about 4,000 miles a year')
        self.assertContains(response, '-3,000, mileage discrepancy')
        self.assertContains(response, '(+11,000)')
        self.assertContains(response, 'Brake pipe corroded')
        self.assertContains(response, 'Tyre worn close to legal limit')
        self.assertContains(response, 'Raw API response')
        self.assertContains(response, 'class="vc-more-btn"', count=2)
        self.assertNotContains(response, 'Save to Profile')
        self.assertNotContains(response, 'Browse Services')

    def test_rows_in_order_with_plain_dvla_values(self, fetch, available):
        fetch.return_value = (Result('ok', DVLA), Result('ok', MOT), Result('ok', LEZ_OK))
        html = self.client.get('/AB12CDE').content.decode()
        labels = ('Registration', 'Year', 'Engine', 'Fuel', 'Colour',
                  'V5C issued', 'MOT', 'Tax', 'London ULEZ / Scottish LEZ',
                  'Insurance')
        shown = re.findall(r'<span class="vehicle-label[^"]*">([^<]+)</span>', html)
        self.assertEqual(shown, list(labels))
        self.assertIn('<span class="vehicle-value">AB12CDE</span>', html)
        self.assertIn('<span class="vehicle-value">1968cc</span>', html)
        self.assertIn('<span class="vehicle-value">Diesel</span>', html)
        self.assertIn('<span>Blue</span><span class="vehicle-sub">(as registered)</span>', html)
        self.assertLess(html.index('London ULEZ'), html.index('id="motHistory"'))

    def test_not_found(self, fetch, available):
        fetch.return_value = (Result('not_found', None), Result('error', None), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, 'lookup/index.html')
        self.assertContains(
            response, 'No vehicle found for AB12CDE', status_code=404)
        self.assertContains(response, 'value="AB12CDE"', status_code=404)

    def test_bad_dvla_key(self, fetch, available):
        fetch.return_value = (Result('auth', None), Result('ok', MOT), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertContains(
            response, 'DVLA rejected the API key', status_code=503)

    def test_mot_failure_is_flagged_not_hidden(self, fetch, available):
        fetch.return_value = (Result('ok', DVLA), Result('error', None), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertContains(response, 'load MOT history from DVSA')
        self.assertContains(response, 'alert-warning')
        self.assertContains(response, 'MOT History (0)')
        self.assertNotContains(response, NO_MOT_HISTORY)
        self.assertNotContains(response, 'Mileage at each MOT')

    def test_no_mot_history(self, fetch, available):
        fetch.return_value = (Result('ok', DVLA), Result('not_found', None), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertContains(response, NO_MOT_HISTORY)
        self.assertContains(response, 'alert-info')

    def test_ulez_not_compliant(self, fetch, available):
        fetch.return_value = (
            Result('ok', DVLA), Result('ok', MOT), Result('ok', dict(LEZ_OK, s='n')))
        response = self.client.get('/AB12CDE')
        self.assertContains(
            response, '<span class="text-danger">Not compliant</span>')

    def test_ulez_unavailable_falls_back_to_tfl(self, fetch, available):
        fetch.return_value = (
            Result('ok', DVLA), Result('ok', MOT), Result('error', None))
        response = self.client.get('/AB12CDE')
        self.assertContains(
            response, 'href="https://tfl.gov.uk/modes/driving/check-your-vehicle/"')
        self.assertContains(response, 'data-copy-reg="AB12CDE"', count=2)
        self.assertContains(response, 'emissions checker didn')
        self.assertNotContains(response, 'LEZ (Scotland)')

    def test_page_asks_for_the_insurance_check_itself(self, fetch, available):
        fetch.return_value = (Result('ok', DVLA), Result('ok', MOT), Result('ok', LEZ_OK))
        with patch('lookup.views.insurance.check') as check:
            response = self.client.get('/AB12CDE')
        check.assert_not_called()
        self.assertContains(response, 'id="insurance" data-url="/AB12CDE/insurance"')
        self.assertContains(response, f'data-token="{response.context["csrf_token"]}"')
        self.assertContains(response, 'id="insuranceMore" hidden')
        self.assertContains(response, f'href="{insurance.MIB_PAGE}"')
        self.assertContains(response, 'data-copy-reg="AB12CDE"', count=1)

    def test_insurance_row_only_links_to_mib_without_a_browser(self, fetch, available):
        available.return_value = False
        fetch.return_value = (Result('ok', DVLA), Result('ok', MOT), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertContains(response, f'href="{insurance.MIB_PAGE}"')
        self.assertContains(response, 'Check on MIB')
        self.assertContains(response, 'data-copy-reg="AB12CDE"', count=1)
        self.assertNotContains(response, 'id="insurance"')
        self.assertNotContains(response, 'data-url')
        self.assertNotContains(response, 'data-token')


class DetailsTests(SimpleTestCase):
    def details(self, dvla=DVLA, mot=MOT, mot_status='ok', **kwargs):
        return build_details(
            dvla, mot, mot_status=mot_status, today=TODAY, **kwargs)

    def test_heading_and_plain_rows(self):
        context = self.details()
        self.assertEqual(context['registration'], 'AB12CDE')
        self.assertEqual(context['make'], 'VOLKSWAGEN')
        self.assertEqual(context['model'], 'GOLF')
        self.assertEqual(context['vc_year'], '2015')
        self.assertEqual(context['colour'], 'Blue')
        self.assertEqual(context['fuel'], 'Diesel')
        self.assertEqual(context['engine'], '1968cc')

    def test_ages_and_dates(self):
        context = self.details()
        self.assertEqual(context['vc_age'], '(11 years, 6 months old)')
        self.assertEqual(context['vc_v5c'], '10/06/2021')
        self.assertEqual(context['vc_v5c_ago'], '(5 years, 3 months ago)')
        self.assertEqual(
            context['vc_mot']['more'], 'Expires 15/01/2027, 108 days remaining')
        self.assertEqual(
            context['vc_tax']['more'], 'Expires 01/03/2027, 153 days remaining')

    def test_fuel_and_logo_names(self):
        context = self.details(
            dict(DVLA, fuelType='ELECTRICITY', make='LAND ROVER'), None)
        self.assertEqual(context['fuel'], 'Electric')
        self.assertEqual(context['make_logo'], 'land_rover')
        self.assertEqual(
            self.details(dict(DVLA, fuelType='HYBRID ELECTRIC'))['fuel'],
            'Hybrid electric')
        self.assertEqual(
            self.details(dict(DVLA, make='MERCEDES'), None)['make_logo'],
            'mercedes_benz')
        self.assertEqual(
            self.details(dict(DVLA, make='ROLLS-ROYCE'), None)['make_logo'],
            'rolls_royce')

    def test_rows_with_nothing_to_show_are_blank(self):
        dvla = {'registrationNumber': 'AB12CDE', 'make': 'TESLA',
                'fuelType': 'ELECTRICITY', 'yearOfManufacture': 2024}
        context = self.details(dvla, None)
        self.assertEqual(
            (context['engine'], context['colour'], context['model']),
            ('', '', ''))
        for key in ('vc_v5c', 'vc_mot', 'vc_tax', 'vc_mot_tests'):
            self.assertNotIn(key, context)

    def test_mot_reply_can_be_a_list(self):
        context = self.details(DVLA, [MOT])
        self.assertEqual(context['model'], 'GOLF')
        self.assertEqual(len(context['vc_mot_tests']), 3)
        self.assertEqual(self.details(DVLA, [])['model'], '')

    def test_age_falls_back_to_dvla_year(self):
        context = self.details(DVLA, None, mot_status='error')
        self.assertEqual(context['vc_age'], '(11 years, 9 months old)')
        self.assertTrue(context['mot_problem'])
        self.assertIn('load MOT history from DVSA', context['mot_notice'])

    def test_mot_notice(self):
        self.assertEqual(self.details()['mot_notice'], '')
        self.assertFalse(self.details()['mot_problem'])
        context = self.details(DVLA, None, mot_status='not_found')
        self.assertEqual(context['mot_notice'], NO_MOT_HISTORY)
        self.assertFalse(context['mot_problem'])

    def test_raw_json_is_untouched_and_inputs_not_mutated(self):
        dvla, mot = copy.deepcopy(DVLA), copy.deepcopy(MOT)
        context = build_details(dvla, mot, today=TODAY)
        self.assertEqual((dvla, mot), (DVLA, MOT))
        self.assertIn('"taxDueDate": "2027-03-01"', context['raw_json'])
        self.assertIn('"completedDate": "2024-01-10T10:00:00.000Z"', context['raw_json'])
        self.assertIn('"lez_scotland": null', context['raw_json'])

    def test_ulez_answer_or_reason(self):
        tfl = 'https://tfl.gov.uk/modes/driving/check-your-vehicle/'
        self.assertEqual(
            self.details(lez=LEZ_OK, lez_status='ok')['vc_ulez'],
            {'ok': True, 'label': 'Compliant'})
        self.assertEqual(
            self.details(lez=dict(LEZ_OK, s='n'), lez_status='ok')['vc_ulez'],
            {'ok': False, 'label': 'Not compliant'})
        reasons = (
            (dict(LEZ_OK, s='u'), 'not_found', '(not recognised by the emissions checker)'),
            (None, 'busy', '(emissions checker busy)'),
            (None, 'error', "(emissions checker didn't answer)"),
            (None, '', ''),
        )
        for lez, status, notice in reasons:
            self.assertEqual(
                self.details(lez=lez, lez_status=status)['vc_ulez'],
                {'ok': None, 'link': tfl, 'notice': notice}, status)


class ChecksTests(SimpleTestCase):
    def shown(self, dvla=DVLA, mot=MOT, lez=None, today=TODAY):
        return checks.display(checks.facts(dvla, mot, lez), today)

    def tax_table(self, today=TODAY, mot=None, **dvla):
        return checks.tax_table(checks.facts(dict(DVLA, **dvla), mot), today)

    def ulez(self, letter, mot=None, **dvla):
        mot = {'registrationDate': mot} if mot else {}
        return self.shown(dict(DVLA, **dvla), mot, ('ok', letter))['vc_ulez']

    def test_countdown(self):
        self.assertEqual(
            checks.countdown(date(2026, 9, 30), TODAY, 'x'), '1 day remaining')
        self.assertEqual(
            checks.countdown(TODAY, TODAY, 'due today'), 'due today')
        self.assertEqual(
            checks.countdown(date(2026, 9, 28), TODAY, 'x'), '1 day overdue')
        self.assertEqual(
            checks.countdown(date(2026, 9, 24), TODAY, 'x'), '5 days overdue')

    def test_span(self):
        self.assertEqual(checks.span(date(2025, 8, 25), TODAY), '1 year, 1 month')
        self.assertEqual(checks.span(date(2025, 9, 29), TODAY), '1 year')
        self.assertEqual(checks.span(date(2026, 8, 30), TODAY), '1 month')
        self.assertEqual(checks.span(date(2026, 9, 20), TODAY), '0 months')

    def test_mot_valid(self):
        self.assertEqual(self.shown()['vc_mot'], {
            'ok': True, 'label': 'Valid',
            'detail': '(expires 15/01/2027, 108 days remaining)',
            'more': 'Expires 15/01/2027, 108 days remaining',
        })

    def test_mot_expired(self):
        dvla = dict(DVLA, motStatus='Not valid', motExpiryDate='2026-09-01')
        mot = self.shown(dvla)['vc_mot']
        self.assertEqual((mot['ok'], mot['label']), (False, 'Not valid'))
        self.assertEqual(mot['more'], 'Expired 01/09/2026, 28 days overdue')

    def test_new_car_first_mot(self):
        dvla = dict(DVLA, motStatus='No details held by DVLA')
        dvla.pop('motExpiryDate')
        mot = self.shown(dvla, {'motTestDueDate': '2027-05-01'})['vc_mot']
        self.assertEqual((mot['ok'], mot['label']), (True, 'Exempt (new vehicle)'))
        self.assertEqual(mot['more'], 'First MOT due 01/05/2027, 214 days remaining')

    def test_new_car_first_mot_overdue(self):
        dvla = dict(DVLA, motStatus='No details held by DVLA')
        mot = self.shown(dvla, {'motTestDueDate': '2026-09-01'})['vc_mot']
        self.assertEqual((mot['ok'], mot['label']), (False, 'Not valid'))
        self.assertEqual(mot['more'], 'Expired 01/09/2026, 28 days overdue')

    def test_mot_with_no_details_at_all(self):
        dvla = dict(DVLA, motStatus='No details held by DVLA')
        mot = self.shown(dvla, None)['vc_mot']
        self.assertEqual((mot['ok'], mot['label']), (None, 'No details held'))
        self.assertNotIn('more', mot)

    def test_mot_status_from_the_history_when_dvla_gives_none(self):
        dvla = {key: value for key, value in DVLA.items()
                if key not in ('motStatus', 'motExpiryDate')}
        self.assertEqual(self.shown(dvla)['vc_mot']['label'], 'Not valid')
        recent = copy.deepcopy(MOT)
        recent['motTests'][0]['expiryDate'] = '2027-01-15'
        mot = self.shown(dvla, recent)['vc_mot']
        self.assertEqual(
            (mot['label'], mot['more']),
            ('Valid', 'Expires 15/01/2027, 108 days remaining'))

    def test_year_from_the_mot_record_when_dvla_gives_none(self):
        dvla = {key: value for key, value in DVLA.items()
                if key != 'yearOfManufacture'}
        self.assertEqual(self.shown(dvla)['vc_year'], '2015')

    def test_tax_statuses(self):
        tax = self.shown()['vc_tax']
        self.assertEqual((tax['ok'], tax['label']), (True, 'Taxed'))
        self.assertEqual(tax['more'], 'Expires 01/03/2027, 153 days remaining')
        tax = self.shown(dict(DVLA, taxStatus='Untaxed', taxDueDate='2026-09-01'))['vc_tax']
        self.assertEqual((tax['ok'], tax['label']), (False, 'Untaxed'))
        self.assertEqual(tax['more'], 'Expired 01/09/2026, 28 days overdue')
        sorn = dict(DVLA, taxStatus='SORN')
        sorn.pop('taxDueDate')
        tax = self.shown(sorn)['vc_tax']
        self.assertEqual((tax['ok'], tax['label']), (False, 'SORN'))
        self.assertNotIn('more', tax)
        self.assertEqual(tax['table']['rows'], [['Standard', '£35']])

    def test_tax_table_by_co2_band(self):
        self.assertEqual(
            self.tax_table(),
            {'cols': ['12 months'], 'rows': [['Standard', '£35']]})
        self.assertEqual(
            self.tax_table(co2Emissions=128),
            {'cols': ['12 months', '6 months'],
             'rows': [['Standard', '£170', '£93.50']]})

    def test_tax_table_uses_the_exact_date_from_the_mot_record(self):
        dvla = dict(monthOfFirstRegistration='2017-03', co2Emissions=128)
        self.assertEqual(
            self.tax_table(mot={'registrationDate': '2017-04-02'}, **dvla)['rows'],
            [['Standard', '£200', '£110']])
        self.assertEqual(
            self.tax_table(mot={'registrationDate': '2017-03-31'}, **dvla)['rows'],
            [['Standard', '£170', '£93.50']])

    def test_tax_table_expensive_car_rate_for_six_years(self):
        self.assertEqual(
            self.tax_table(monthOfFirstRegistration='2022-06', yearOfManufacture=2022,
                           fuelType='PETROL'),
            {'cols': ['12 months', '6 months'],
             'rows': [['Standard', '£200', '£110'],
                      ['List price over £40k', '£640', '£352']]})
        self.assertEqual(
            self.tax_table(monthOfFirstRegistration='2025-05', yearOfManufacture=2025,
                           fuelType='ELECTRICITY', co2Emissions=0)['rows'][1],
            ['List price over £50k', '£640', '£352'])
        self.assertEqual(
            self.tax_table(monthOfFirstRegistration='2025-03', yearOfManufacture=2025,
                           fuelType='ELECTRICITY', co2Emissions=0)['rows'][1][0],
            'List price over £40k')
        self.assertEqual(
            self.tax_table(monthOfFirstRegistration='2018-01', yearOfManufacture=2018,
                           fuelType='PETROL')['rows'],
            [['Standard', '£200', '£110']])
        last_day = dict(mot={'registrationDate': '2020-09-30'},
                        monthOfFirstRegistration='2020-09')
        self.assertEqual(len(self.tax_table(**last_day)['rows']), 2)
        self.assertEqual(
            len(self.tax_table(date(2026, 9, 30), **last_day)['rows']), 1)

    def test_tax_table_band_k_turns_on_the_day_in_march_2006(self):
        big = dict(monthOfFirstRegistration='2006-03', yearOfManufacture=2006,
                   co2Emissions=300, fuelType='PETROL')
        self.assertIsNone(self.tax_table(**big))
        self.assertEqual(
            self.tax_table(mot={'registrationDate': '2006-03-10'}, **big)['rows'],
            [['Standard', '£445', '£244.75']])
        self.assertEqual(
            self.tax_table(mot={'registrationDate': '2006-03-23'}, **big)['rows'],
            [['Standard', '£790', '£434.50']])

    def test_tax_table_only_where_every_rule_is_known(self):
        self.assertEqual(
            self.tax_table(yearOfManufacture=1985),
            {'note': 'Historic vehicle: exempt from tax'})
        self.assertIsNotNone(self.tax_table(yearOfManufacture=1986))
        self.assertIsNone(self.tax_table(typeApproval='N1'))
        self.assertIsNone(self.tax_table(typeApproval=''))
        self.assertIsNone(self.tax_table(monthOfFirstRegistration=''))
        self.assertIsNone(self.tax_table(date(2026, 3, 31)))
        self.assertIsNotNone(self.tax_table(date(2026, 4, 1)))
        self.assertIsNotNone(self.tax_table(date(2027, 3, 31)))
        self.assertIsNone(self.tax_table(date(2027, 4, 1)))
        tax = self.shown(dict(DVLA, typeApproval='N1'))['vc_tax']
        self.assertNotIn('table', tax)
        self.assertEqual(tax['more'], 'Expires 01/03/2027, 153 days remaining')

    def test_ulez_straight_answers(self):
        self.assertEqual(self.ulez('c'), {'ok': True, 'label': 'Compliant'})
        self.assertEqual(self.ulez('n'), {'ok': False, 'label': 'Not compliant'})
        unrecognised = self.ulez('u')
        self.assertIsNone(unrecognised['ok'])
        self.assertEqual(unrecognised['link'], checks.TFL_ULEZ_URL)
        self.assertEqual(
            unrecognised['notice'], '(not recognised by the emissions checker)')

    def test_ulez_exempt_answer_goes_by_the_cars_own_age(self):
        compliant = {'ok': True, 'label': 'Compliant'}
        unclear = '(no automatic answer for this vehicle)'
        self.assertEqual(self.ulez('e', '2015-09-01'), compliant)
        self.assertEqual(self.ulez('e', '2015-08-31')['notice'], unclear)
        self.assertEqual(self.ulez('e', '2006-01-01', fuelType='PETROL'), compliant)
        self.assertEqual(
            self.ulez('e', '2005-12-31', fuelType='PETROL')['notice'], unclear)
        self.assertEqual(
            self.ulez('e', '2015-08-31', fuelType='HYBRID ELECTRIC')['notice'], unclear)
        self.assertEqual(self.ulez('e', '2012-05-01', fuelType='ELECTRICITY'), compliant)
        self.assertEqual(self.ulez('e', '2020-01-01', fuelType='GAS')['notice'], unclear)
        self.assertEqual(self.ulez('e', '2020-01-01', typeApproval='')['notice'], unclear)
        self.assertEqual(self.ulez('e', '2020-01-01', typeApproval='M2')['notice'], unclear)

    def test_ulez_exempt_answer_for_vans(self):
        compliant = {'ok': True, 'label': 'Compliant'}
        self.assertIsNone(self.ulez('e', '2016-08-31', typeApproval='N1')['ok'])
        self.assertEqual(self.ulez('e', '2016-09-01', typeApproval='N1'), compliant)
        self.assertIsNone(
            self.ulez('e', '2006-12-31', typeApproval='N1', fuelType='PETROL')['ok'])
        self.assertEqual(
            self.ulez('e', '2007-01-01', typeApproval='N1', fuelType='PETROL'),
            compliant)

    def test_ulez_exempt_answer_without_the_exact_date(self):
        compliant = {'ok': True, 'label': 'Compliant'}
        self.assertEqual(self.ulez('e', monthOfFirstRegistration='2015-09'), compliant)
        self.assertIsNone(self.ulez('e', monthOfFirstRegistration='2015-08')['ok'])
        year_only = dict(monthOfFirstRegistration='')
        self.assertEqual(self.ulez('e', yearOfManufacture=2016, **year_only), compliant)
        self.assertIsNone(self.ulez('e', yearOfManufacture=2015, **year_only)['ok'])

    def test_ulez_motorbikes_go_by_londons_own_rule(self):
        compliant = {'ok': True, 'label': 'Compliant'}
        bike = dict(typeApproval='L3e', fuelType='PETROL')
        for letter in ('c', 'e', 'n'):
            self.assertEqual(self.ulez(letter, '2007-07-01', **bike), compliant)
            old = self.ulez(letter, '2007-06-30', **bike)
            self.assertIsNone(old['ok'])
            self.assertEqual(old['notice'], '(no automatic answer for this vehicle)')
        self.assertEqual(
            self.ulez('n', '2001-01-01', typeApproval='L1e', fuelType='ELECTRICITY'),
            compliant)
        self.assertEqual(
            self.ulez('u', '2010-01-01', **bike)['notice'],
            '(not recognised by the emissions checker)')

    def test_ulez_when_the_checker_gave_nothing(self):
        for status, notice in (('busy', '(emissions checker busy)'),
                               ('error', "(emissions checker didn't answer)"),
                               ('', '')):
            answer = self.shown(lez=(status, ''))['vc_ulez']
            self.assertEqual(
                answer, {'ok': None, 'link': checks.TFL_ULEZ_URL, 'notice': notice})

    def test_mot_history_newest_first_with_mileage_changes(self):
        tests = self.shown()['vc_mot_tests']
        self.assertEqual(
            [t['date'] for t in tests], ['12/01/2025', '10/01/2024', '09/01/2023'])
        self.assertEqual([t['passed'] for t in tests], [False, True, True])
        self.assertEqual(tests[1]['mileage'], '61,000 miles')
        self.assertEqual((tests[0]['diff'], tests[0]['diff_negative']), ('-3,000', True))
        self.assertEqual((tests[1]['diff'], tests[1]['diff_negative']), ('+11,000', False))
        self.assertNotIn('diff', tests[2])

    def test_mot_history_compares_like_with_like(self):
        mot = copy.deepcopy(MOT)
        mot['motTests'][1].update(odometerValue='98000', odometerUnit='KM')
        mot['motTests'][2]['odometerValue'] = 'unreadable'
        tests = self.shown(mot=mot)['vc_mot_tests']
        self.assertEqual(tests[0]['mileage'], '98,000 km')
        self.assertNotIn('diff', tests[0])
        self.assertNotIn('diff', tests[1])
        self.assertEqual(tests[2]['mileage'], '')

    def test_defects_grouped_and_capitalised(self):
        mot = {'registrationDate': '2015-03-20', 'motTests': [{
            'completedDate': '2025-01-12T10:00:00.000Z',
            'testResult': 'FAILED',
            'odometerValue': '58000',
            'odometerUnit': 'MI',
            'defects': [
                {'text': 'brake pipe corroded', 'type': 'MAJOR'},
                {'text': 'tyre cords exposed', 'type': 'DANGEROUS'},
                {'text': 'exhaust leaking', 'type': 'FAIL'},
                {'text': 'headlamp aim too high', 'type': 'PRS'},
                {'text': 'tyre worn close to legal limit', 'type': 'ADVISORY'},
                {'text': 'number plate lamp inoperative', 'type': 'MINOR'},
                {'text': 'child seat fitted', 'type': 'USER ENTERED'},
                {'text': '', 'type': 'ADVISORY'},
            ],
        }]}
        test = self.shown(mot=mot)['vc_mot_tests'][0]
        self.assertEqual(test['fails'], [
            {'text': 'Brake pipe corroded', 'dangerous': False},
            {'text': 'Tyre cords exposed', 'dangerous': True},
            {'text': 'Exhaust leaking', 'dangerous': False},
        ])
        self.assertEqual(test['repaired'], ['Headlamp aim too high'])
        self.assertEqual(test['advisories'], [
            'Tyre worn close to legal limit',
            'Number plate lamp inoperative',
            'Child seat fitted',
        ])

    def test_mileage_chart(self):
        chart = self.shown()['vc_mileage_chart']
        self.assertEqual(chart['tests'], 3)
        self.assertEqual(chart['avg'], '4,000')
        self.assertEqual(
            chart['summary'],
            'Mileage at 3 MOT tests, from 50,000 miles in 2023 to 58,000 miles in 2025')
        self.assertEqual([v['results'] for v in chart['visits']], [[True], [True], [False]])
        self.assertEqual([v['drop'] for v in chart['visits']], [False, False, True])
        self.assertEqual(
            chart['visits'][2]['lines'],
            ['12/01/2025 · failed', '58,000 miles', 'Lower than the test before'])
        self.assertEqual(
            [tick['label'] for tick in chart['yticks']], ['0', '20k', '40k', '60k', '80k'])
        self.assertEqual(
            [tick['label'] for tick in chart['xticks']], ['2023', '2024', '2025'])
        for visit in chart['visits']:
            self.assertTrue(0 <= float(visit['x']) <= 100)
            self.assertTrue(0 <= float(visit['y']) <= 100)

    def test_mileage_chart_joins_a_fail_and_its_retest(self):
        mot = copy.deepcopy(MOT)
        mot['motTests'].append({
            'completedDate': '2025-01-20T10:00:00.000Z',
            'testResult': 'PASSED',
            'expiryDate': '2026-01-19',
            'odometerValue': '100000',
            'odometerUnit': 'KM',
        })
        chart = self.shown(mot=mot)['vc_mileage_chart']
        self.assertEqual(chart['tests'], 4)
        self.assertEqual(
            [v['results'] for v in chart['visits']], [[True], [True], [False, True]])
        last = chart['visits'][-1]
        self.assertEqual(
            last['lines'],
            ['12/01/2025 failed · 20/01/2025 passed', '100,000 km'])
        self.assertFalse(last['drop'])
        self.assertIn('to 100,000 km in 2025', chart['summary'])

    def test_mileage_chart_needs_two_readings(self):
        mot = copy.deepcopy(MOT)
        mot['motTests'] = mot['motTests'][:1]
        shown = self.shown(mot=mot)
        self.assertEqual(len(shown['vc_mot_tests']), 1)
        self.assertNotIn('vc_mileage_chart', shown)

    def test_display_never_raises(self):
        with self.assertLogs('lookup.checks', 'ERROR'):
            self.assertEqual(checks.display({'mot_tests': 'nonsense'}, TODAY), {})
        self.assertEqual(checks.display({}, TODAY), {})
        self.assertEqual(checks.display(None, TODAY), {})


@patch.dict(os.environ, API_ENV)
class ServiceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @patch('lookup.services.requests.post')
    def test_dvla_ok(self, post):
        post.return_value = fake_response(200, DVLA)
        self.assertEqual(fetch_dvla('AB12CDE'), Result('ok', DVLA))
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://dvla.example/vehicles')
        self.assertEqual(kwargs['json'], {'registrationNumber': 'AB12CDE'})
        self.assertEqual(kwargs['headers']['x-api-key'], 'dvla-key')
        self.assertTrue(kwargs['timeout'])

    @patch('lookup.services.requests.post')
    def test_dvla_failures(self, post):
        for code, status in ((404, 'not_found'), (400, 'not_found'),
                             (403, 'auth'), (429, 'busy'), (500, 'error')):
            post.return_value = fake_response(code)
            self.assertEqual(fetch_dvla('AB12CDE').status, status, code)
        post.return_value = fake_response(200, ValueError('not json'))
        self.assertEqual(fetch_dvla('AB12CDE').status, 'error')
        post.side_effect = requests.Timeout
        self.assertEqual(fetch_dvla('AB12CDE').status, 'error')

    def test_dvla_not_configured(self):
        with patch.dict(os.environ, {'DVLA_API_KEY': ''}):
            self.assertEqual(fetch_dvla('AB12CDE').status, 'not_configured')

    @patch('lookup.services.requests.get')
    @patch('lookup.services.requests.post')
    def test_mot_token_is_reused(self, post, get):
        post.return_value = fake_response(
            200, {'access_token': 'tok', 'expires_in': 3599})
        get.return_value = fake_response(200, MOT)
        self.assertEqual(fetch_mot('AB12CDE'), Result('ok', MOT))
        self.assertEqual(fetch_mot('AB12CDE'), Result('ok', MOT))
        self.assertEqual(post.call_count, 1)
        args, kwargs = get.call_args
        self.assertEqual(
            args[0], 'https://mot.example/v1/trade/vehicles/registration/AB12CDE')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer tok')
        self.assertEqual(kwargs['headers']['X-API-Key'], 'mot-key')

    @patch('lookup.services.requests.get')
    @patch('lookup.services.requests.post')
    def test_mot_stale_token_retried_once(self, post, get):
        post.side_effect = [
            fake_response(200, {'access_token': 'old'}),
            fake_response(200, {'access_token': 'new'}),
        ]
        get.side_effect = [fake_response(401), fake_response(200, MOT)]
        self.assertEqual(fetch_mot('AB12CDE'), Result('ok', MOT))
        self.assertEqual(
            get.call_args[1]['headers']['Authorization'], 'Bearer new')

    @patch('lookup.services.requests.get')
    @patch('lookup.services.requests.post')
    def test_mot_failures(self, post, get):
        post.return_value = fake_response(400)
        self.assertEqual(fetch_mot('AB12CDE').status, 'error')
        get.assert_not_called()
        post.return_value = fake_response(200, {'access_token': 'tok'})
        get.return_value = fake_response(404)
        self.assertEqual(fetch_mot('AB12CDE').status, 'not_found')

    def test_mot_not_configured(self):
        with patch.dict(os.environ, {'MOT_API_KEY': ''}):
            self.assertEqual(fetch_mot('AB12CDE').status, 'not_configured')

    @patch('lookup.services.fetch_lez', return_value=Result('ok', LEZ_OK))
    @patch('lookup.services.fetch_mot', return_value=Result('ok', MOT))
    @patch('lookup.services.fetch_dvla', return_value=Result('ok', DVLA))
    def test_fetch_vehicle_asks_all_three(self, dvla, mot, lez):
        self.assertEqual(
            fetch_vehicle('AB12CDE'),
            (Result('ok', DVLA), Result('ok', MOT), Result('ok', LEZ_OK)))

    @patch('lookup.services.requests.post')
    def test_lez_ok(self, post):
        post.return_value = fake_response(200, {'vehicleResult': [LEZ_OK]})
        self.assertEqual(fetch_lez('AB12CDE'), Result('ok', LEZ_OK))
        args, kwargs = post.call_args
        self.assertEqual(
            args[0], 'https://vehicleemissionscheck.service.gov.scot/api')
        self.assertEqual(kwargs['json'], {'vrn': 'AB12CDE'})
        self.assertTrue(kwargs['timeout'])

    @patch('lookup.services.requests.post')
    def test_lez_other_answers(self, post):
        post.return_value = fake_response(
            200, {'vehicleResult': [dict(LEZ_OK, s='u')]})
        self.assertEqual(fetch_lez('AB12CDE').status, 'not_found')
        post.return_value = fake_response(429)
        self.assertEqual(fetch_lez('AB12CDE').status, 'busy')
        with self.assertLogs('lookup.services', 'WARNING'):
            for reply in (fake_response(500),
                          fake_response(200, ValueError('not json')),
                          fake_response(200, {'vehicleResult': []}),
                          fake_response(200, {'vehicleResult': [{'s': 'x'}]})):
                post.return_value = reply
                self.assertEqual(fetch_lez('AB12CDE').status, 'error')
            post.side_effect = requests.Timeout
            self.assertEqual(fetch_lez('AB12CDE').status, 'error')


MIB_INSURED = (
    'Result\n'
    'Vehicle Registration Number: AB12 CDE\n'
    'This vehicle is showing as INSURED in Navigate today\n'
    'Make and model: VOLKSWAGEN GOLF\n'
    'Check another vehicle'
)
CHECKED = datetime(2026, 10, 10, 13, 32, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = 5000.0

    def monotonic(self):
        return self.now


class InsuranceTestCase(SimpleTestCase):
    def setUp(self):
        insurance._answers.clear()
        insurance._started.clear()
        insurance._pause.update(until=0.0, status='')
        self.clock = Clock()
        for target, value in (('time', self.clock), ('available', lambda: True)):
            patcher = patch(f'lookup.insurance.{target}', value)
            patcher.start()
            self.addCleanup(patcher.stop)


class InsuranceParseTests(SimpleTestCase):
    def test_insured_with_the_vehicle_mib_names(self):
        self.assertEqual(
            insurance._parse(MIB_INSURED, 'AB12CDE'),
            {'status': 'insured', 'vehicle': 'VOLKSWAGEN GOLF'})
        self.assertEqual(
            insurance._parse(MIB_INSURED.replace('Make and model', 'Model'), 'AB12CDE'),
            {'status': 'insured', 'vehicle': ''})

    def test_not_insured_in_either_wording(self):
        for wording in ('NOT INSURED', 'UNINSURED', 'not  insured'):
            text = MIB_INSURED.replace('as INSURED', f'as {wording}')
            self.assertEqual(
                insurance._parse(text, 'AB12CDE'), {'status': 'uninsured'}, wording)

    def test_anything_else_is_no_answer(self):
        unknown = {'status': 'unknown'}
        self.assertEqual(insurance._parse(MIB_INSURED, 'XY34ZZZ'), unknown)
        self.assertEqual(insurance._parse('', 'AB12CDE'), unknown)
        self.assertEqual(
            insurance._parse(MIB_INSURED.replace('in Navigate today', ''), 'AB12CDE'),
            unknown)
        self.assertEqual(
            insurance._parse(
                MIB_INSURED.replace('Vehicle Registration Number', 'Reg'), 'AB12CDE'),
            unknown)


@patch('lookup.insurance.lookup')
class InsuranceCheckTests(InsuranceTestCase):
    def test_answer_is_kept_for_an_hour(self, lookup):
        lookup.return_value = {'status': 'insured', 'vehicle': 'VOLKSWAGEN GOLF'}
        first = insurance.check('AB12CDE')
        self.assertEqual(
            (first['status'], first['vehicle']), ('insured', 'VOLKSWAGEN GOLF'))
        self.assertIsNotNone(first['checked'].tzinfo)
        self.clock.now += insurance.ANSWER_SECONDS - 1
        self.assertEqual(insurance.check('AB12CDE'), first)
        lookup.assert_called_once_with('AB12CDE')
        self.clock.now += 2
        insurance.check('AB12CDE')
        self.assertEqual(lookup.call_count, 2)

    def test_each_registration_has_its_own_answer(self, lookup):
        lookup.side_effect = [{'status': 'insured', 'vehicle': ''},
                              {'status': 'uninsured'}]
        self.assertEqual(insurance.check('AB12CDE')['status'], 'insured')
        self.assertEqual(insurance.check('XY34ZZZ')['status'], 'uninsured')
        self.assertEqual(insurance.check('AB12CDE')['status'], 'insured')
        self.assertEqual(lookup.call_count, 2)

    def test_failures_are_not_kept(self, lookup):
        lookup.return_value = {'status': 'unknown'}
        self.assertEqual(insurance.check('AB12CDE'), {'status': 'unknown'})
        self.assertEqual(insurance.check('AB12CDE'), {'status': 'unknown'})
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(insurance._answers, {})

    def test_no_browser_means_no_lookup(self, lookup):
        with patch('lookup.insurance.available', return_value=False):
            self.assertEqual(insurance.check('AB12CDE'), {'status': 'unavailable'})
        lookup.assert_not_called()

    def test_mibs_limit_pauses_every_check(self, lookup):
        lookup.return_value = {'status': 'limit'}
        self.assertEqual(insurance.check('AB12CDE'), {'status': 'limit'})
        lookup.return_value = {'status': 'insured', 'vehicle': ''}
        self.clock.now += insurance.PAUSES['limit'] - 1
        self.assertEqual(insurance.check('XY34ZZZ'), {'status': 'limit'})
        self.assertEqual(lookup.call_count, 1)
        self.clock.now += 2
        self.assertEqual(insurance.check('XY34ZZZ')['status'], 'insured')
        self.assertEqual(lookup.call_count, 2)

    def test_browser_that_will_not_start_is_left_alone_for_a_while(self, lookup):
        lookup.return_value = {'status': 'unavailable'}
        for _ in range(insurance.MAX_PER_DAY + 1):
            self.assertEqual(insurance.check('AB12CDE'), {'status': 'unavailable'})
        self.assertEqual(lookup.call_count, 1)
        lookup.return_value = {'status': 'uninsured'}
        self.clock.now += insurance.PAUSES['unavailable'] + 1
        self.assertEqual(insurance.check('AB12CDE')['status'], 'uninsured')
        self.assertEqual(lookup.call_count, 2)

    def test_hourly_cap(self, lookup):
        lookup.return_value = {'status': 'unknown'}
        for number in range(insurance.MAX_PER_HOUR):
            self.assertEqual(insurance.check(f'AB{number:02}CDE')['status'], 'unknown')
        self.assertEqual(insurance.check('XY34ZZZ'), {'status': 'capped'})
        self.assertEqual(lookup.call_count, insurance.MAX_PER_HOUR)
        self.clock.now += 60 * 60
        self.assertEqual(insurance.check('XY34ZZZ')['status'], 'unknown')

    def test_daily_cap(self, lookup):
        lookup.return_value = {'status': 'unknown'}
        for number in range(insurance.MAX_PER_DAY):
            self.clock.now += 20 * 60
            self.assertEqual(insurance.check(f'AB{number:02}CDE')['status'], 'unknown')
        self.clock.now += 61 * 60
        self.assertEqual(insurance.check('XY34ZZZ'), {'status': 'capped'})
        self.clock.now += 24 * 60 * 60
        self.assertEqual(insurance.check('XY34ZZZ')['status'], 'unknown')

    def test_kept_answers_do_not_use_up_the_cap(self, lookup):
        lookup.return_value = {'status': 'insured', 'vehicle': ''}
        for _ in range(insurance.MAX_PER_DAY * 2):
            self.assertEqual(insurance.check('AB12CDE')['status'], 'insured')
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(len(insurance._started), 1)

    def test_busy_while_another_check_holds_the_browser(self, lookup):
        with patch('lookup.insurance.TURN_SECONDS', 0.01):
            with insurance._one_at_a_time:
                self.assertEqual(insurance.check('AB12CDE'), {'status': 'busy'})
        lookup.assert_not_called()
        lookup.return_value = {'status': 'uninsured'}
        self.assertEqual(insurance.check('AB12CDE')['status'], 'uninsured')

    def test_two_at_once_for_one_registration_share_a_lookup(self, lookup):
        started, finish, answers = threading.Event(), threading.Event(), []

        def slow(registration):
            started.set()
            finish.wait(5)
            return {'status': 'insured', 'vehicle': ''}

        lookup.side_effect = slow
        threads = [
            threading.Thread(target=lambda: answers.append(insurance.check('AB12CDE')))
            for _ in range(2)
        ]
        threads[0].start()
        self.assertTrue(started.wait(5))
        threads[1].start()
        finish.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(lookup.call_count, 1)

    def test_browser_is_released_when_the_lookup_blows_up(self, lookup):
        lookup.side_effect = RuntimeError
        with self.assertRaises(RuntimeError):
            insurance.check('AB12CDE')
        self.assertFalse(insurance._one_at_a_time.locked())


class InsuranceLookupTests(InsuranceTestCase):
    def playwright(self, launch):
        manager = MagicMock()
        manager.__enter__.return_value = Mock(chromium=Mock(launch=launch))
        module = Mock(sync_playwright=Mock(return_value=manager))
        return patch.dict(
            sys.modules, {'playwright': Mock(), 'playwright.sync_api': module})

    def test_reads_the_result_and_closes_the_browser(self):
        browser = Mock()
        launch = Mock(return_value=browser)
        with self.playwright(launch), \
                patch('lookup.insurance._click_through', return_value=MIB_INSURED) as click, \
                self.assertLogs('lookup.insurance', 'INFO') as logs:
            self.assertEqual(
                insurance.lookup('AB12CDE'),
                {'status': 'insured', 'vehicle': 'VOLKSWAGEN GOLF'})
        launch.assert_called_once_with(headless=insurance.HEADLESS)
        click.assert_called_once_with(browser.new_page.return_value, 'AB12CDE')
        browser.close.assert_called_once_with()
        self.assertEqual(logs.output, ['INFO:lookup.insurance:MIB lookup finished: insured'])

    def test_browser_that_will_not_start(self):
        launch = Mock(side_effect=RuntimeError("Executable doesn't exist"))
        with self.playwright(launch), self.assertLogs('lookup.insurance', 'ERROR'):
            self.assertEqual(insurance.lookup('AB12CDE'), {'status': 'unavailable'})

    def test_page_that_does_not_go_as_expected(self):
        browser = Mock()
        with self.playwright(Mock(return_value=browser)), \
                patch('lookup.insurance._click_through', side_effect=TimeoutError), \
                patch('lookup.insurance._log_what_mib_showed') as showed, \
                self.assertLogs('lookup.insurance', 'ERROR'):
            self.assertEqual(insurance.lookup('AB12CDE'), {'status': 'unknown'})
        showed.assert_called_once()
        browser.close.assert_called_once_with()

    def test_mibs_search_limit(self):
        browser = Mock()
        with self.playwright(Mock(return_value=browser)), \
                patch('lookup.insurance._click_through',
                      side_effect=insurance.SearchLimitReached), \
                patch('lookup.insurance._log_what_mib_showed') as showed, \
                self.assertLogs('lookup.insurance', 'WARNING'):
            self.assertEqual(insurance.lookup('AB12CDE'), {'status': 'limit'})
        showed.assert_not_called()
        browser.close.assert_called_once_with()

    def test_headless_unless_told_otherwise(self):
        self.assertIs(insurance.HEADLESS, os.environ.get(
            'MIB_HEADLESS', 'true').strip().lower() != 'false')
        self.assertEqual(insurance.MIB_PAGE, 'https://enquiry.navigate.mib.org.uk/checkyourvehicle')


class InsuranceRowTests(SimpleTestCase):
    def test_insured(self):
        row = insurance.row(
            {'status': 'insured', 'vehicle': 'VOLKSWAGEN GOLF', 'checked': CHECKED})
        self.assertEqual(row, {
            'status': 'insured',
            'ok': True,
            'label': 'Insured',
            'more': ['MIB shows it as insured today',
                     'MIB lists it as VOLKSWAGEN GOLF',
                     'Checked at 14:32'],
        })

    def test_not_insured(self):
        row = insurance.row({'status': 'uninsured', 'checked': CHECKED})
        self.assertEqual(row, {
            'status': 'uninsured',
            'ok': False,
            'label': 'Not insured',
            'more': ['MIB shows it as not insured today', 'Checked at 14:32'],
        })

    def test_no_answer(self):
        self.assertEqual(insurance.row({'status': 'limit'}), {
            'status': 'limit',
            'ok': None,
            'notice': "(MIB's search limit has been reached, try again later)",
            'retry': False,
        })
        for status, retry in (('unknown', True), ('busy', True),
                              ('capped', False), ('unavailable', False)):
            row = insurance.row({'status': status})
            self.assertEqual((row['ok'], row['retry']), (None, retry), status)
            self.assertEqual(row['notice'], f'({insurance.NOTICES[status]})')


@patch('lookup.views.insurance.check')
class InsuranceEndpointTests(SimpleTestCase):
    def test_post_runs_the_check_for_that_registration(self, check):
        check.return_value = {
            'status': 'insured', 'vehicle': 'VOLKSWAGEN GOLF', 'checked': CHECKED}
        response = self.client.post('/AB12CDE/insurance')
        check.assert_called_once_with('AB12CDE')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'status': 'insured',
            'ok': True,
            'label': 'Insured',
            'more': ['MIB shows it as insured today',
                     'MIB lists it as VOLKSWAGEN GOLF',
                     'Checked at 14:32'],
        })

    def test_no_answer_is_still_a_reply(self, check):
        check.return_value = {'status': 'busy'}
        response = self.client.post('/AB12CDE/insurance')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['retry'], True)

    def test_get_never_checks(self, check):
        self.assertEqual(self.client.get('/AB12CDE/insurance').status_code, 405)
        check.assert_not_called()

    def test_only_clean_registrations(self, check):
        for path in ('/ab12cde/insurance', '/AB12%20CDE/insurance',
                     '/ABCDEFG/insurance', '/AB12CDEF/insurance',
                     '/AB12CDE/insurance/', '/insurance'):
            self.assertEqual(self.client.post(path).status_code, 404, path)
        check.assert_not_called()

    @patch('lookup.views.insurance.available', return_value=True)
    @patch('lookup.views.fetch_vehicle')
    def test_needs_the_pages_own_token(self, fetch, available, check):
        check.return_value = {'status': 'uninsured', 'checked': CHECKED}
        fetch.return_value = (Result('ok', DVLA), Result('ok', MOT), Result('ok', LEZ_OK))
        browser = Client(enforce_csrf_checks=True)
        self.assertEqual(browser.post('/AB12CDE/insurance').status_code, 403)
        check.assert_not_called()
        token = str(browser.get('/AB12CDE').context['csrf_token'])
        response = browser.post('/AB12CDE/insurance', headers={'X-CSRFToken': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['label'], 'Not insured')
        check.assert_called_once_with('AB12CDE')

    @override_settings(SITE_PASSWORD='correct horse')
    def test_locked_with_the_rest_of_the_site(self, check):
        self.assertRedirects(
            self.client.post('/AB12CDE/insurance'),
            '/unlock/?next=%2FAB12CDE%2Finsurance', fetch_redirect_response=False)
        check.assert_not_called()


class TaxTests(SimpleTestCase):
    def test_post_2017_standard_rate(self):
        tax = get_annual_tax(120, 'PETROL', 2019, 6, 1498)
        self.assertEqual((tax['annual_rate'], tax['six_month_rate']), (200, 110))

    def test_2001_to_2017_bands(self):
        self.assertEqual(get_annual_tax(119, 'DIESEL', 2015, 3, 1968)['annual_rate'], 35)
        self.assertEqual(get_annual_tax(0, 'ELECTRICITY', 2014, 5, None)['annual_rate'], 20)
        tax = get_annual_tax(300, 'PETROL', 2010, 1, 4000)
        self.assertEqual((tax['band'], tax['annual_rate'], tax['six_month_rate']), ('M', 790, 434.50))

    def test_band_k_covers_big_engines_before_march_2006(self):
        tax = get_annual_tax(290, 'PETROL', 2005, 11, 3200)
        self.assertEqual((tax['band'], tax['annual_rate']), ('K', 445))
        self.assertEqual(get_annual_tax(290, 'PETROL', 2006, 4, 3200)['band'], 'M')

    def test_early_2001_goes_by_engine_size(self):
        self.assertEqual(get_annual_tax(180, 'PETROL', 2001, 2, 1800)['annual_rate'], 375)
        self.assertEqual(get_annual_tax(180, 'PETROL', 2001, 3, 1800)['band'], 'I')

    def test_pre_2001_engine_size(self):
        self.assertEqual(get_annual_tax(None, 'PETROL', 1998, 8, 1400)['annual_rate'], 230)
        self.assertEqual(get_annual_tax(None, 'PETROL', 1998, 8, 1796)['annual_rate'], 375)


class PasswordGateTests(SimpleTestCase):
    def test_open_when_no_password(self):
        with self.settings(SITE_PASSWORD=''):
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertRedirects(
                self.client.get('/unlock/'), '/',
                fetch_redirect_response=False)

    @override_settings(SITE_PASSWORD='correct horse')
    def test_locked_until_password_given(self):
        self.assertRedirects(
            self.client.get('/AB12CDE'), '/unlock/?next=%2FAB12CDE',
            fetch_redirect_response=False)
        self.assertEqual(self.client.get('/unlock/').status_code, 200)

        wrong = self.client.post(
            '/unlock/', {'password': 'nope', 'next': '/'})
        self.assertContains(wrong, 'Wrong password')

        right = self.client.post(
            '/unlock/', {'password': 'correct horse', 'next': '/'})
        self.assertRedirects(right, '/', fetch_redirect_response=False)
        self.assertEqual(self.client.get('/').status_code, 200)

        with self.settings(SITE_PASSWORD='new password'):
            self.assertEqual(self.client.get('/').status_code, 302)

    @override_settings(SITE_PASSWORD='correct horse')
    def test_password_asked_again_after_a_day(self):
        self.client.post('/unlock/', {'password': 'correct horse', 'next': '/'})
        self.assertEqual(self.client.get('/').status_code, 200)
        with patch('django.core.signing.time.time', return_value=time.time() + 23 * 3600):
            self.assertEqual(self.client.get('/').status_code, 200)
        with patch('django.core.signing.time.time', return_value=time.time() + 25 * 3600):
            self.assertRedirects(self.client.get('/'), '/unlock/?next=%2F',
                                 fetch_redirect_response=False)

    @override_settings(SITE_PASSWORD='correct horse')
    def test_no_open_redirect(self):
        response = self.client.post('/unlock/', {
            'password': 'correct horse', 'next': 'https://evil.example/'})
        self.assertRedirects(response, '/', fetch_redirect_response=False)

    @override_settings(SITE_PASSWORD='correct horse')
    def test_static_files_not_locked(self):
        response = self.client.get('/static/css/style.css')
        self.assertNotEqual(response.status_code, 302)


class ErrorPageTests(SimpleTestCase):
    def test_404_page(self):
        response = self.client.get('/nothing-here')
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, '404.html')
        self.assertContains(response, 'Look up a vehicle', status_code=404)

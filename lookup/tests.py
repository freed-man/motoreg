import copy
import os
import types
from datetime import date
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from . import insurance
from .details import (
    NO_MOT_HISTORY, build_details, countdown_text, span_text, ulez_verdict,
)
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
    'motTests': [  # deliberately out of order
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


@patch('lookup.views.fetch_vehicle')
class ResultPageTests(SimpleTestCase):
    def test_non_canonical_urls_redirect(self, fetch):
        for path in ('/ab12cde', '/AB12CDE/', '/AB12%20CDE', '/ab12 cde/'):
            response = self.client.get(path)
            self.assertRedirects(
                response, '/AB12CDE', fetch_redirect_response=False)
        fetch.assert_not_called()

    def test_junk_paths_404(self, fetch):
        for path in ('/admin', '/robots.txt', '/wp-login.php',
                     '/ABCDEFGH12', '/AB1.2'):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        fetch.assert_not_called()

    def test_full_result(self, fetch):
        fetch.return_value = (Result('ok', DVLA), Result('ok', MOT), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        fetch.assert_called_once_with('AB12CDE')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'lookup/result.html')
        self.assertContains(response, '<title>AB12CDE | motoreg</title>')
        self.assertContains(response, '2015 VOLKSWAGEN GOLF')
        self.assertContains(response, 'images/makes/volkswagen.png')
        self.assertContains(response, 'MOT History (3)')
        self.assertContains(response, 'mileage discrepancy')
        self.assertContains(response, '(+11,000)')
        self.assertContains(response, 'Brake pipe corroded')
        self.assertContains(response, 'Tyre worn close to legal limit')
        self.assertContains(response, 'est. annual tax: £35')
        self.assertContains(response, 'Raw API response')
        self.assertNotContains(response, 'data-copy-reg="AB12CDE"')
        self.assertContains(response, 'id="insurance-button"')
        self.assertContains(response, '<span class="text-success">Compliant</span>')
        self.assertNotContains(response, 'Save to Profile')
        self.assertNotContains(response, 'Browse Services')

    def test_not_found(self, fetch):
        fetch.return_value = (Result('not_found', None), Result('error', None), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, 'lookup/index.html')
        self.assertContains(
            response, 'No vehicle found for AB12CDE', status_code=404)
        self.assertContains(response, 'value="AB12CDE"', status_code=404)

    def test_bad_dvla_key(self, fetch):
        fetch.return_value = (Result('auth', None), Result('ok', MOT), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertContains(
            response, 'DVLA rejected the API key', status_code=503)

    def test_mot_failure_is_flagged_not_hidden(self, fetch):
        fetch.return_value = (Result('ok', DVLA), Result('error', None), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertContains(response, 'load MOT history from DVSA')
        self.assertContains(response, 'alert-warning')
        self.assertNotContains(response, NO_MOT_HISTORY)

    def test_no_mot_history(self, fetch):
        fetch.return_value = (Result('ok', DVLA), Result('not_found', None), Result('ok', LEZ_OK))
        response = self.client.get('/AB12CDE')
        self.assertContains(response, NO_MOT_HISTORY)
        self.assertContains(response, 'alert-info')

    def test_ulez_not_compliant(self, fetch):
        fetch.return_value = (
            Result('ok', DVLA), Result('ok', MOT), Result('ok', dict(LEZ_OK, s='n')))
        response = self.client.get('/AB12CDE')
        self.assertContains(
            response, '<span class="text-danger">Not compliant</span>')

    def test_ulez_unavailable_falls_back_to_tfl(self, fetch):
        fetch.return_value = (
            Result('ok', DVLA), Result('ok', MOT), Result('error', None))
        response = self.client.get('/AB12CDE')
        self.assertContains(
            response, 'href="https://tfl.gov.uk/modes/driving/check-your-vehicle/"')
        self.assertContains(response, 'data-copy-reg="AB12CDE"', count=1)
        self.assertContains(response, 'emissions checker didn')
        self.assertNotContains(response, 'LEZ (Scotland)')

    def test_colour_fuel_case_and_status_order(self, fetch):
        fetch.return_value = (Result('ok', DVLA), Result('ok', MOT), Result('ok', LEZ_OK))
        html = self.client.get('/AB12CDE').content.decode()
        self.assertIn('<strong>Colour:</strong> Blue</p>', html)
        self.assertIn('<strong>Fuel Type:</strong> Diesel</p>', html)
        spots = [html.index(f'<strong>{label}:</strong>')
                 for label in ('MOT', 'Tax', 'ULEZ', 'Insurance')]
        self.assertEqual(spots, sorted(spots))


class DetailsTests(SimpleTestCase):
    def details(self, dvla=DVLA, mot=MOT, mot_status='ok'):
        return build_details(dvla, mot, mot_status=mot_status, today=TODAY)

    def test_countdowns_and_ages(self):
        context = self.details()
        self.assertEqual(context['vehicle_age_text'], '11 years, 6 months old')
        self.assertEqual(context['mot_days_text'], '108 days remaining')
        self.assertEqual(context['tax_days_text'], '153 days remaining')
        self.assertEqual(context['v5c_days_text'], '5 years, 3 months')
        self.assertEqual(context['dvla']['motExpiryDateFormatted'], '15/01/2027')
        self.assertEqual(context['dvla']['v5cDateFormatted'], '10/06/2021')

    def test_countdown_text(self):
        self.assertEqual(
            countdown_text(date(2026, 9, 30), TODAY, 'x'), '1 day remaining')
        self.assertEqual(countdown_text(TODAY, TODAY, 'due today'), 'due today')
        self.assertEqual(
            countdown_text(date(2026, 9, 28), TODAY, 'x'), '1 day overdue')
        self.assertEqual(
            countdown_text(date(2026, 9, 24), TODAY, 'x'), '5 days overdue')

    def test_span_text(self):
        self.assertEqual(span_text(date(2025, 8, 25), TODAY), '1 year, 1 month')
        self.assertEqual(span_text(date(2025, 9, 29), TODAY), '1 year')
        self.assertEqual(span_text(date(2026, 8, 30), TODAY), '1 month')
        self.assertEqual(span_text(date(2026, 9, 20), TODAY), '0 months')

    def test_mot_tests_sorted_with_mileage_diffs(self):
        tests = self.details()['mot_tests']
        self.assertEqual(
            [t['completedDateFormatted'] for t in tests],
            ['12/01/2025', '10/01/2024', '09/01/2023'])
        self.assertEqual(tests[0]['mileage_diff'], -3000)
        self.assertEqual(tests[1]['mileage_diff'], 11000)
        self.assertNotIn('mileage_diff', tests[2])
        self.assertEqual(tests[1]['mileageFormatted'], '61,000')
        self.assertEqual(tests[1]['odometerUnit'], 'miles')

    def test_defects_grouped_and_capitalised(self):
        tests = self.details()['mot_tests']
        self.assertEqual(tests[0]['majors'][0]['text'], 'Brake pipe corroded')
        self.assertEqual(tests[0]['advisories'], [])
        self.assertEqual(
            tests[1]['advisories'][0]['text'], 'Tyre worn close to legal limit')

    def test_new_car_first_mot(self):
        dvla = dict(DVLA, motStatus='No details held by DVLA')
        dvla.pop('motExpiryDate')
        mot = {'model': 'ID.3', 'motTestDueDate': '2027-05-01'}
        context = self.details(dvla, mot)
        self.assertTrue(context['new_car_mot'])
        self.assertEqual(context['new_car_mot_date'], '01/05/2027')
        self.assertEqual(context['new_car_mot_days'], '214 days remaining')
        self.assertEqual(context['mot_notice'], NO_MOT_HISTORY)

    def test_new_car_first_mot_overdue(self):
        dvla = dict(DVLA, motStatus='No details held by DVLA')
        mot = {'motTestDueDate': '2026-09-01'}
        self.assertEqual(
            self.details(dvla, mot)['new_car_mot_days'], '28 days overdue')

    def test_fuel_and_logo_names(self):
        context = self.details(
            dict(DVLA, fuelType='ELECTRICITY', make='LAND ROVER'), None)
        self.assertEqual(context['fuel_display'], 'ELECTRIC')
        self.assertEqual(context['make_logo'], 'land_rover')
        self.assertEqual(
            self.details(dict(DVLA, make='MERCEDES'), None)['make_logo'],
            'mercedes_benz')
        self.assertEqual(
            self.details(dict(DVLA, make='ROLLS-ROYCE'), None)['make_logo'],
            'rolls_royce')

    def test_age_falls_back_to_dvla_year(self):
        context = self.details(DVLA, None, mot_status='error')
        self.assertEqual(context['vehicle_age_text'], '11 years, 9 months old')
        self.assertTrue(context['mot_problem'])

    def test_raw_json_is_untouched_and_inputs_not_mutated(self):
        dvla, mot = copy.deepcopy(DVLA), copy.deepcopy(MOT)
        context = build_details(dvla, mot, today=TODAY)
        self.assertEqual((dvla, mot), (DVLA, MOT))
        self.assertIn('"taxDueDate": "2027-03-01"', context['raw_json'])
        self.assertNotIn('Formatted', context['raw_json'])

    def test_ulez_verdicts(self):
        self.assertEqual(ulez_verdict(LEZ_OK), 'compliant')
        self.assertEqual(ulez_verdict(dict(LEZ_OK, s='n')), 'not_compliant')
        for status in ('e', 'u', 'x'):
            self.assertEqual(ulez_verdict(dict(LEZ_OK, s=status)), '')

    def test_ulez_answer_or_reason(self):
        self.assertEqual(self.details()['ulez_verdict'], '')
        context = build_details(DVLA, MOT, lez=dict(LEZ_OK, s='n'),
                                lez_status='ok', today=TODAY)
        self.assertEqual(
            (context['ulez_verdict'], context['ulez_notice']), ('not_compliant', ''))
        context = build_details(DVLA, MOT, lez=dict(LEZ_OK, s='e'),
                                lez_status='ok', today=TODAY)
        self.assertEqual(context['ulez_notice'], 'no automatic answer for this vehicle')
        context = build_details(DVLA, MOT, lez_status='busy', today=TODAY)
        self.assertIn('busy', context['ulez_notice'])
        self.assertIn('"lez_scotland": null', context['raw_json'])


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


class TaxTests(SimpleTestCase):
    """Spot checks against GOV.UK's 2026/27 tables."""

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



class InsuranceTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def post(self, reg='AB12CDE'):
        return self.client.post(f'/{reg}/insurance', HTTP_X_REQUESTED_WITH='fetch')

    @patch('lookup.views.fetch_vehicle')
    def test_button_on_every_car(self, fetch):
        fetch.side_effect = lambda reg: (
            Result('ok', dict(DVLA, registrationNumber=reg)), Result('ok', MOT),
            Result('ok', LEZ_OK))
        for reg in ('AB12CDE', 'NC15ABC'):
            page = self.client.get(f'/{reg}')
            self.assertContains(page, 'id="insurance-button"')
            self.assertContains(page, f'data-url="/{reg}/insurance"')
            self.assertContains(page, f'data-reg="{reg}"')
            self.assertContains(page, 'csrfmiddlewaretoken')
            self.assertContains(page, 'your car?')

    def test_only_real_regs_can_be_checked(self):
        self.assertEqual(self.post('ABCDEFGH12').status_code, 404)
        self.assertEqual(self.client.get('/ABCDEFGH12/insurance').status_code, 404)
        self.assertEqual(self.client.post('/admin/insurance').status_code, 404)

    @patch('lookup.insurance.threading.Thread')
    def test_one_check_at_a_time(self, thread):
        self.assertEqual(self.client.get('/AB12CDE/insurance').json(), {'state': 'none'})
        self.assertEqual(self.post().json(), {'state': 'running'})
        self.assertEqual(self.post().json(), {'state': 'running'})
        self.assertEqual(thread.call_count, 1)

    @patch('lookup.insurance.threading.Thread')
    def test_second_car_waits_its_turn(self, thread):
        self.post()
        self.assertEqual(self.post('CD34EFG').json(), {'state': 'busy'})

    @patch('lookup.insurance.threading.Thread')
    def test_plain_form_post_goes_back_to_the_page(self, thread):
        response = self.client.post('/AB12CDE/insurance')
        self.assertRedirects(response, '/AB12CDE', fetch_redirect_response=False)

    @patch('lookup.insurance.run_check')
    def test_answer_is_kept_and_not_rechecked(self, run_check):
        run_check.return_value = {
            'status': 'INSURED', 'make_model': 'FORD FOCUS',
            'site_time': '14:32:10 29 September 2026', 'detail': 'INSURED'}
        with patch('lookup.insurance.threading.Thread') as thread:
            thread.side_effect = lambda target, args, daemon: types.SimpleNamespace(
                start=lambda: target(*args))
            self.post()
            state = self.client.get('/AB12CDE/insurance').json()
            self.assertEqual((state['state'], state['status'], state['make_model']),
                             ('done', 'INSURED', 'FORD FOCUS'))
            self.assertIsNone(cache.get(insurance.LOCK_KEY))
            self.assertEqual(self.post().json()['status'], 'INSURED')
            self.assertEqual(thread.call_count, 1)

    @patch('lookup.insurance.run_check', side_effect=RuntimeError('boom'))
    def test_a_crash_is_reported_not_left_running(self, run_check):
        with self.assertLogs('lookup.insurance', 'ERROR'):
            insurance._job('AB12CDE')
        self.assertEqual(insurance.status('AB12CDE')['status'], 'ERROR')

    def test_human_check_stops_the_script(self):
        fake = types.SimpleNamespace(DEFAULT_URL='https://example.test',
                                     pause=lambda message: None)

        def check_one(page, reg, url, out_dir, timeout):
            try:
                fake.pause('The site wants a human check')
            except Exception as exc:
                return {'status': 'ERROR', 'detail': f'{type(exc).__name__}: {exc}'}
            return {'status': 'INSURED', 'detail': 'pause did not stop it'}

        fake.check_one = check_one
        with patch('lookup.insurance.load_script', return_value=fake), \
                patch('lookup.insurance.browser_page') as page:
            page.return_value.__enter__.return_value = 'page'
            result = insurance.run_check('AB12CDE')
        self.assertTrue(result['detail'].startswith('NeedsHuman'))
        with patch('lookup.insurance.run_check', return_value=result):
            insurance._job('AB12CDE')
        self.assertEqual(insurance.status('AB12CDE')['status'], 'HUMAN_CHECK')


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

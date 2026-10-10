TAX_YEAR = '2026/27'

FIRST_YEAR_STANDARD = [
    (0, 10), (50, 115), (75, 135), (90, 280), (100, 365),
    (110, 405), (130, 455), (150, 560), (170, 1410),
    (190, 2270), (225, 3420), (255, 4850),
]
FIRST_YEAR_STANDARD_OVER_255 = 5690

FIRST_YEAR_DIESEL = [
    (0, 10), (50, 135), (75, 280), (90, 365), (100, 405),
    (110, 455), (130, 560), (150, 1410), (170, 2270),
    (190, 3420), (225, 4850), (255, 5690),
]
FIRST_YEAR_DIESEL_OVER_255 = 5690

STANDARD_RATE_12 = 200
STANDARD_RATE_6 = 110

EXPENSIVE_SUPPLEMENT = 440

RATES_2001_2017 = [
    (100, 20, None),
    (110, 20, None),
    (120, 35, None),
    (130, 170, 93.50),
    (140, 200, 110),
    (150, 225, 123.75),
    (165, 275, 151.25),
    (175, 325, 178.75),
    (185, 360, 198),
    (200, 410, 225.50),
    (225, 445, 244.75),
    (255, 760, 418),
]
RATES_2001_2017_OVER_255 = (790, 434.50)

PRE_2001_SMALL = 230
PRE_2001_SMALL_6M = 126.50
PRE_2001_LARGE = 375
PRE_2001_LARGE_6M = 206.25


def get_annual_tax(co2, fuel_type, reg_year, reg_month, engine_cc=None):
    result = {
        'annual_rate': None,
        'six_month_rate': None,
        'first_year_rate': None,
        'band': '',
        'note': '',
    }

    if reg_year is None:
        result['note'] = 'Registration date unknown'
        return result

    if reg_year > 2017 or (reg_year == 2017 and reg_month and reg_month >= 4):
        result['annual_rate'] = STANDARD_RATE_12
        result['six_month_rate'] = STANDARD_RATE_6
        result['note'] = 'Standard rate'

        if co2 is not None:
            if fuel_type == 'DIESEL':
                rates = FIRST_YEAR_DIESEL
                over = FIRST_YEAR_DIESEL_OVER_255
            else:
                rates = FIRST_YEAR_STANDARD
                over = FIRST_YEAR_STANDARD_OVER_255

            first_year = over
            for threshold, rate in rates:
                if co2 <= threshold:
                    first_year = rate
                    break
            result['first_year_rate'] = first_year

        return result

    if reg_year > 2001 or (reg_year == 2001 and not (reg_month and reg_month < 3)):
        if co2 is not None:
            if co2 > 225 and (reg_year, reg_month or 12) < (2006, 3):
                co2 = 225
            bands = 'ABCDEFGHIJKLM'
            found = False
            for i, (threshold, annual, six_month) in enumerate(RATES_2001_2017):
                if co2 <= threshold:
                    result['annual_rate'] = annual
                    result['six_month_rate'] = six_month
                    result['band'] = bands[i]
                    found = True
                    break
            if not found:
                result['annual_rate'] = RATES_2001_2017_OVER_255[0]
                result['six_month_rate'] = RATES_2001_2017_OVER_255[1]
                result['band'] = 'M'
        result['note'] = 'Based on CO2 emissions'
        return result

    if engine_cc is not None:
        if engine_cc <= 1549:
            result['annual_rate'] = PRE_2001_SMALL
            result['six_month_rate'] = PRE_2001_SMALL_6M
        else:
            result['annual_rate'] = PRE_2001_LARGE
            result['six_month_rate'] = PRE_2001_LARGE_6M
        result['note'] = 'Based on engine size'
    return result

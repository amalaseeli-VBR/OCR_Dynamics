"""Tell whether an extracted value is printed on the PDF or was worked out by DocMate.

Printed values are sent as invoice data; values DocMate calculated or added (a due
date from payment terms, a default currency, a recalculated VAT) go to
additionalFields. Matching is format-tolerant: '2,071.17' matches 2071.17 and
'June 29, 2022' matches 2022-06-29.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import re


_NUMBER_TOKEN = re.compile(r'\d[\d,.]*')
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
           'september', 'october', 'november', 'december']


def _alnum(text):
    return re.sub(r'[^0-9a-z]', '', str(text).casefold())


def _decimals(token):
    """All readings of a printed number: 1,234.50 / 1.234,50 / 16,50 / 2.026.240."""
    token = token.strip('.,')
    readings = {token.replace(',', '')}
    if ',' in token:
        readings.add(token.replace('.', '').replace(',', '.'))
    if token.count('.') > 1:
        readings.add(token.replace('.', ''))
    result = set()
    for reading in readings:
        try:
            result.add(abs(Decimal(reading)))
        except InvalidOperation:
            pass
    return result


def _date_forms(value):
    d = date.fromisoformat(value)
    days = {str(d.day), f'{d.day:02d}'}
    days |= {day + suffix for day in (str(d.day),) for suffix in ('st', 'nd', 'rd', 'th')}
    months_num = {str(d.month), f'{d.month:02d}'}
    month_name = _MONTHS[d.month - 1]
    months_txt = {month_name, month_name[:3], month_name[:4]}
    years = {str(d.year)}
    forms = set()
    for day in days:
        for month in months_num | months_txt:
            for year in years | {str(d.year)[2:]}:
                if year == str(d.year)[2:] and (month in months_num and len(day) == 1):
                    continue  # 1/6/22-style forms are too short to trust
                forms |= {day + month + year, month + day + year, year + month + day}
    return {_alnum(f) for f in forms}


class PdfEvidence:
    def __init__(self, text):
        self.text_alnum = _alnum(text or '')
        self.numbers = set()
        for token in _NUMBER_TOKEN.findall(text or ''):
            self.numbers |= _decimals(token)
        self.has_zero = Decimal(0) in self.numbers

    @property
    def available(self):
        return len(self.text_alnum) >= 20

    def printed(self, value, zero_counts=True):
        """True when the value appears on the PDF; None when it cannot be judged."""
        if not self.available or value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float, Decimal)):
            return self._number(value, zero_counts)
        if isinstance(value, (list, dict)):
            return None
        text = str(value).strip()
        if not text:
            return None
        if _ISO_DATE.match(text):
            try:
                if any(form in self.text_alnum for form in _date_forms(text)):
                    return True
            except ValueError:
                pass
        try:
            if self._number(Decimal(text.replace(',', '').replace('£', '')), zero_counts):
                return True
        except (InvalidOperation, ValueError):
            pass
        key = _alnum(text)
        return bool(key) and key in self.text_alnum

    def _number(self, value, zero_counts):
        number = abs(Decimal(str(value)))
        if number == 0:
            return self.has_zero and zero_counts
        return number in self.numbers

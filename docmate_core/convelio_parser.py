"""Convelio order invoices in PDF text or OCR reading order."""
import re
from decimal import Decimal


def parse_convelio(text):
    """Return an app invoice envelope, or None for an unsupported layout.

    Restrict parsing to the invoice table; bank details and terms are not rows.
    Both interleaved OCR totals and PDF labels followed by values are supported.
    """
    if not re.search(r'convelio\.com', text, re.I):
        return None
    table = re.search(
        r'Description\s+Qty\s+Unit Price \(excl\. VAT\)\s+'
        r'VAT:\s*\([^)]*\)\s+Total \(incl\. VAT\)(.*?)'
        r'Subtotal \(without taxes\)(.*)', text, re.S | re.I)
    if not table:
        return None
    number = re.compile(r'([-+]?\d[\d,]*(?:\.\d+)?)\s*[€£$]?')

    def amount(value):
        match = number.fullmatch(value.strip())
        return Decimal(match[1].replace(',', '')) if match else None

    rows, description, values = [], [], []
    for token in table[1].splitlines():
        token = token.strip()
        if not token:
            continue
        value = amount(token)
        if value is None:
            # OCR can place wrapped description text after the numeric cells.
            description.append(token)
            continue
        if len(values) == 4:
            # Multiple rows need spatial grouping that this layout reader lacks.
            return None
        values.append(value)
    if len(values) != 4 or not description:
        return None
    quantity, price, vat, gross = values
    net = quantity * price
    if abs(net + vat - gross) > Decimal('0.02'):
        return None
    total_values = []
    for token in table[2].splitlines():
        if token.strip() in ('VAT:', 'Total (incl. VAT)', ''):
            continue
        value = amount(token)
        if value is None:
            break
        total_values.append(value)
        if len(total_values) == 3:
            break
    if len(total_values) != 3:
        return None
    total_net, total_vat, total_gross = total_values
    if any(abs(a - b) > Decimal('0.02') for a, b in
           zip((net, vat, gross), total_values)):
        return None
    invoice = re.search(r'\bINV[\w]*-\d+\b', text)
    dates = re.findall(r'\b\d{4}-\d{2}-\d{2}\b', text[:table.start()])
    vat_no = re.search(r'VAT:\s*(FR\w+)', text)
    if not invoice or len(dates) != 2 or '€' not in table[0]:
        return None
    rows.append({'lineNo': 1, 'description': ' '.join(description),
                 'qty': float(quantity), 'unitPrice': float(price),
                 'lineNet': float(net), 'vatAmount': float(vat)})
    return {'header': {'supplierName': 'Convelio',
                       'supplierVatNo': vat_no[1] if vat_no else '',
                       'invoiceNumber': invoice[0], 'invoiceDate': dates[0],
                       'dueDate': dates[1], 'currency': 'EUR',
                       'totalNet': float(total_net), 'totalVat': float(total_vat),
                       'totalGross': float(total_gross)}, 'items': rows}

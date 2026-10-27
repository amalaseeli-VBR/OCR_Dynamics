"""Map worker records before SQL persistence, retaining extraction evidence."""
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .invoice_mapping import map_invoice
from .bc_table_mapping import map_bc_tables


def load_customer_mapping(path):
    """An absent default file leaves master data unresolved; invalid files fail."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f'BC customer mapping file not found: {path}')
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict):
        raise ValueError('BC customer mapping must be a JSON object.')
    for key in ('suppliers', 'defaults'):
        if not isinstance(config.get(key, {}), dict):
            raise ValueError(f'BC mapping {key} must be an object.')
    if any(not isinstance(v, dict) for v in config.get('suppliers', {}).values()):
        raise ValueError('Each supplier mapping must be an object.')
    return config


def prepare_mapping(header, items, customer_config=None, source_text=None):
    """Return shared mapping plus conservative delivery validation.

    Missing master data is saved for review, never replaced with invented values.
    Endpoint-specific requirements must also be checked by the eventual sender.
    """
    mapped = map_invoice({'header': header, 'items': items}, customer_config)
    master_warnings = [w for w in mapped['warnings'] if w.endswith('is not configured.')]
    errors = [w for w in mapped['warnings'] if w not in master_warnings]
    h, lines = mapped['buffer_header'], mapped['buffer_lines']
    for key in ('InvoiceDate', 'DueDate'):
        if h.get(key):
            try:
                date.fromisoformat(h[key])
            except (TypeError, ValueError):
                errors.append(f'{key} must use YYYY-MM-DD.')
    if not h.get('CurrencyCode'):
        errors.append('CurrencyCode is missing.')

    def numeric(value, label):
        try:
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError()
            return result
        except (InvalidOperation, ValueError):
            errors.append(f'{label} must be a finite number.')
            return None

    totals = [numeric(h.get(k), k) for k in ('TotalNet', 'TotalVat', 'TotalGross')]
    if all(v is not None for v in totals) and abs(totals[0] + totals[1] - totals[2]) > Decimal('0.10'):
        errors.append('Header net plus VAT does not match gross.')
    net_values, numbers = [], set()
    for i, line in enumerate(lines, 1):
        prefix = f'Line {i}'
        number = numeric(line.get('LineNumber'), f'{prefix} LineNumber')
        if number is not None:
            if number <= 0 or number != number.to_integral_value() or number in numbers:
                errors.append(f'{prefix} needs a unique positive integer LineNumber.')
            numbers.add(number)
        for key in ('Quantity', 'UnitPrice'):
            numeric(line.get(key), f'{prefix} {key}')
        net_values.append(numeric(line.get('NetAmount'), f'{prefix} NetAmount'))
        # Line VAT is optional: BC calculates it, and VAT is checked per tax band in map_bc_tables.
        if not line.get('Description'):
            errors.append(f'{prefix} Description is missing.')
        if not line.get('BcGlAccount'):
            master_warnings.append(f'{prefix} BcGlAccount is not configured.')
    if net_values and totals[0] is not None and all(v is not None for v in net_values):
        if abs(sum(net_values) - totals[0]) > Decimal('0.10'):
            errors.append('Line net amounts do not match the header total.')
    mapped['validation'] = {'readyForDelivery': not errors, 'errors': list(dict.fromkeys(errors)),
                            'masterDataWarnings': list(dict.fromkeys(master_warnings))}
    mapped['bc_tables'] = map_bc_tables(header, items, mapped, source_text=source_text)
    # e.g. a discount without a supplier rule, or invoice VAT outside the tolerance: hold for review.
    review = mapped['bc_tables']['reviewReasons']
    if review:
        validation = mapped['validation']
        validation['errors'] = list(dict.fromkeys(validation['errors'] + review))
        validation['readyForDelivery'] = False
    return mapped


def mapped_sql_values(header, items, mapped):
    """Overlay mapped fields for SQL, preserving other extraction attributes."""
    h = dict(header)
    for target, source in {
        'supplierName': 'SupplierName', 'supplierVatNo': 'SupplierVatNo',
        'invoiceNumber': 'InvoiceNumber', 'invoiceDate': 'InvoiceDate',
        'dueDate': 'DueDate', 'currency': 'CurrencyCode', 'totalNet': 'TotalNet',
        'totalVat': 'TotalVat', 'totalGross': 'TotalGross',
    }.items():
        h[target] = mapped['buffer_header'].get(source)
    result = []
    for index, line in enumerate(mapped['buffer_lines']):
        item = dict(items[index]) if index < len(items) else {}
        for target, source in {
            'lineNo': 'LineNumber', 'code': 'ItemCode', 'barcode': 'Barcode',
            'description': 'Description', 'qty': 'Quantity', 'unitPrice': 'UnitPrice',
            'lineNet': 'NetAmount', 'vatRate': 'VatRate', 'vatAmount': 'VatAmount',
        }.items():
            item[target] = line.get(source)
        result.append(item)
    return h, result

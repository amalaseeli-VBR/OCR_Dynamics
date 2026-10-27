"""Shared invoice mapping for preview and Dynamics persistence."""
from .docmate_bc_pipeline import DocmateBcPipeline
from .jsonata_transformer import JsonataTransformer


def mapping_input(raw):
    """Adapt the app's header/items envelope; preserve original extraction separately."""
    if not isinstance(raw.get('header'), dict) or 'supplier' in raw['header']:
        return raw
    adapted = dict(raw['header'])
    for target, aliases in {
        'supplierVatNo': ('vatRegistrationNumber', 'vatNumber', 'supplierVatNumber'),
        'dueDate': ('paymentDueDate',), 'totalGross': ('totalAmount',),
    }.items():
        if adapted.get(target) in (None, ''):
            for alias in aliases:
                if adapted.get(alias) not in (None, ''):
                    adapted[target] = adapted[alias]
                    break
    adapted['items'] = []
    for source in raw.get('items', raw.get('lines', [])):
        item = dict(source)
        for target, aliases in {'lineNet': ('lineAmount',), 'barcode': ('ean', 'gtin')}.items():
            if item.get(target) in (None, ''):
                for alias in aliases:
                    if item.get(alias) not in (None, ''):
                        item[target] = item[alias]
                        break
        adapted['items'].append(item)
    return adapted


def map_invoice(raw, customer_config=None):
    pipeline = DocmateBcPipeline(customer_config=customer_config)
    payload = pipeline.process_invoice(mapping_input(raw))
    _, warnings = JsonataTransformer.validate_canonical(payload)
    header, lines = pipeline.prepare_bc_buffer_records(payload)
    if not lines:
        warnings.append('No invoice lines were extracted or mapped.')
    for field in ('BcVendorNo', 'BcGlAccount'):
        if not header.get(field):
            warnings.append(f'{field} is not configured.')
    for field in ('InvoiceDate', 'TotalNet', 'TotalVat', 'TotalGross'):
        if header.get(field) in (None, ''):
            warnings.append(f'{field} is missing.')
    return {'canonical': payload, 'buffer_header': header,
            'buffer_lines': lines, 'warnings': warnings}

"""Map printed-source evidence using vendor rules and explicit exclusions."""
from .bc_table_mapping import (
    load_field_mapping, _map, _effective_entries, supplier_rules_for, is_excluded_field,
)
from .line_numbers import ensure_line_numbers


def printed_records(capture):
    """Return verified extraction values; only missing line identifiers are generated."""
    printed = capture.get('printedInvoice') or {}
    evidence = printed.get('evidence') or {}

    def verified(values, quotes):
        return {key: value for key, value in values.items()
                if not key.startswith('_')
                and isinstance(quotes.get(key), str) and quotes[key].strip()}

    header = verified(printed.get('header') or {}, evidence.get('header') or {})
    quotes = evidence.get('items') or []
    rows = [verified(row, quotes[i] if i < len(quotes) else {})
            for i, row in enumerate(printed.get('items') or [])]
    return header, ensure_line_numbers(rows)


def printed_payload(capture, mapping_audit=None, field_mapping=None):
    header, rows = printed_records(capture)
    mapping = field_mapping if field_mapping is not None else load_field_mapping()
    rules = supplier_rules_for(mapping, header.get('supplierName'))
    header_entries = _effective_entries(mapping['header'], rules, 'header')
    line_entries = _effective_entries(mapping['lines'], rules, 'lines')

    def missing(values, entries):
        configured = {tag for entry in entries for tag in entry['docmateTags']}
        return sorted(key for key in values if key not in configured and not key.startswith('_'))

    def excluded(values, entries):
        return {tag: {'mappingCode': entry['bcField'], 'value': values[tag]}
                for entry in entries if is_excluded_field(entry['bcField'])
                for tag in entry['docmateTags'] if tag in values}

    if mapping_audit is not None:
        missing_header = missing(header, header_entries)
        missing_lines = [{'row': index, 'tags': tags}
                         for index, row in enumerate(rows, 1)
                         if (tags := missing(row, line_entries))]
        complete = not missing_header and not missing_lines
        mapping_audit.update({
            'unmappedHeaderTags': missing_header,
            'unmappedLineTags': missing_lines,
            'mappingComplete': complete,
            'reason': 'all_tags_configured' if complete else 'no_configured_bc_mapping',
            'action': 'mapped_or_intentionally_excluded' if complete else 'sent_under_original_tag',
            'excludedHeaderTags': excluded(header, header_entries),
            'excludedLineTags': [
                {'row': index, 'tags': tags}
                for index, row in enumerate(rows, 1)
                if (tags := excluded(row, line_entries))],
            'supplierRulesApplied': [
                f"{r['docmateTag']} -> {r['bcField']}" for r in rules],
        })

    def convert(values, entries):
        active = [e for e in entries if not is_excluded_field(e['bcField'])]
        excluded_tags = {tag for e in entries if is_excluded_field(e['bcField'])
                         for tag in e['docmateTags']}
        mapped, used = _map(values, active)
        for key, value in values.items():
            if key not in used and key not in excluded_tags:
                mapped[key] = value
        for entry in active:
            for tag in entry['docmateTags']:
                if tag in values and values[tag] in (None, ''):
                    mapped.setdefault(entry['bcField'], values[tag])
                    mapped.pop(tag, None)
        return mapped

    return convert(header, header_entries), [convert(row, line_entries) for row in rows]

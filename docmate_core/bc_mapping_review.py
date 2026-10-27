"""SQL-backed queue of observed, unconfigured OCR tags."""
import copy
import json
from contextlib import closing
from pathlib import Path

from .bc_table_mapping import (
    FIELD_TYPES, _effective_entries, supplier_rules_for, save_field_mapping,
    validate_field_mapping,
)


def read_mapping_audits():
    # Use the Dynamics worker configuration without importing/starting the worker.
    import pyodbc
    path = Path(__file__).resolve().parent.parent / 'dynamics_db.yaml'
    config = {}
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if line and not line.startswith('#') and ':' in line:
            key, value = line.split(':', 1)
            config[key.strip()] = value.strip().strip('"').strip("'")
    def escaped(value):
        return '{' + str(value).replace('}', '}}') + '}'
    auth = ('UID=' + escaped(config['username']) + ';PWD=' + escaped(config.get('password', '')) + ';'
            if config.get('username') else 'Trusted_Connection=yes;')
    connection_string = (
        'DRIVER=' + escaped(config.get('driver') or 'ODBC Driver 17 for SQL Server')
        + ';SERVER=' + escaped(config.get('server') or '.')
        + ';DATABASE=' + escaped(config.get('database') or 'OcrDynamics') + ';'
        + auth + 'Encrypt=no;TrustServerCertificate=yes;'
    )
    with closing(pyodbc.connect(connection_string, timeout=int(config.get('timeout') or 10))) as conn:
        cursor = conn.cursor()
        cursor.execute("""SELECT InvoiceHeaderId, created_utc, supplier_name,
                          MappingAuditJson, CombinedJson
                          FROM dbo.OCR_InvoiceHeader
                          WHERE MappingAuditJson IS NOT NULL""")
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row)) for row in cursor]


def build_review_queue(records, mapping):
    """Count invoices once per section/tag; resolve against today's configuration."""
    groups, skipped = {}, 0
    for record in records:
        try:
            audit = json.loads(record['MappingAuditJson'])
            coverage = audit.get('bcMappingCoverage')
            if not isinstance(coverage, dict):
                skipped += 1
                continue
            payload = json.loads(record.get('CombinedJson') or '{}')
            supplier = record.get('supplier_name') or '(Unknown supplier)'
            rules = supplier_rules_for(mapping, supplier)
            configured = {
                section: {tag for e in _effective_entries(mapping[section], rules, section)
                          for tag in e['docmateTags']}
                for section in ('header', 'lines')
            }
            observations = [('header', tag, payload.get('invoiceHeader', {}).get(tag))
                            for tag in coverage.get('unmappedHeaderTags', [])]
            lines = payload.get('invoiceLines') or []
            for line in coverage.get('unmappedLineTags', []):
                index = int(line['row']) - 1
                values = lines[index] if 0 <= index < len(lines) else {}
                observations.extend(('lines', tag, values.get(tag)) for tag in line['tags'])
            # Validate before adding any observations from this invoice.
            for section, tag, value in observations:
                if not isinstance(tag, str):
                    raise ValueError('Invalid tag')
        except (ValueError, TypeError, KeyError, AttributeError):
            skipped += 1
            continue
        for section, tag, value in observations:
            if tag in configured[section]:
                continue
            group = groups.setdefault((section, tag), {
                'Section': section, 'OCR field': tag, 'invoices': set(),
                'suppliers': set(), 'dates': [], 'samples': [],
            })
            group['invoices'].add(record['InvoiceHeaderId'])
            group['suppliers'].add(supplier)
            if record.get('created_utc'):
                group['dates'].append(str(record['created_utc']))
            if value is not None:
                sample = str(value)[:200]
                if sample not in group['samples'] and len(group['samples']) < 3:
                    group['samples'].append(sample)
    result = []
    for key in sorted(groups):
        group = groups[key]
        result.append({
            'Section': group['Section'], 'OCR field': group['OCR field'],
            'Invoices affected': len(group['invoices']),
            'Suppliers': ', '.join(sorted(group['suppliers'])),
            'First seen (UTC)': min(group['dates'], default=''),
            'Last seen (UTC)': max(group['dates'], default=''),
            'Sample values': ' | '.join(group['samples']),
        })
    return result, skipped


def add_review_mapping(mapping, section, tag, target, value_type):
    updated = copy.deepcopy(mapping)
    entry = next((e for e in updated[section] if e['bcField'] == target), None)
    if entry:
        if entry['type'] != value_type:
            raise ValueError('Select the existing BC field value type: ' + entry['type'])
        if tag not in entry['docmateTags']:
            entry['docmateTags'].append(tag)
    else:
        updated[section].append({'bcField': target, 'docmateTags': [tag], 'type': value_type})
    errors = validate_field_mapping(updated)
    if errors:
        raise ValueError('; '.join(errors))
    return updated


def render_mapping_review(mapping):
    import pandas as pd
    import streamlit as st
    from .bc_table_mapping import load_field_mapping

    st.subheader('Fields awaiting mapping')
    st.caption('Existing Dynamics invoice audits, checked against the current saved mapping. '
               'First/last seen refer to retained invoice processing timestamps. '
               'Review this queue regularly; no scheduled notifications are configured.')
    if st.button('Refresh invoice audits', key='mapping_review_refresh'):
        st.session_state.pop('mapping_review_records', None)
    if 'mapping_review_records' not in st.session_state:
        try:
            with st.spinner('Reading Dynamics invoice audits...'):
                records = read_mapping_audits()
            st.session_state['mapping_review_records'] = records
        except Exception:
            st.error('Could not read Dynamics invoice audits. Check dynamics_db.yaml, SQL connectivity, '
                     'and read access to dbo.OCR_InvoiceHeader including MappingAuditJson and CombinedJson.')
            return
    records = st.session_state['mapping_review_records']
    suppliers = sorted({r.get('supplier_name') or '(Unknown supplier)' for r in records})
    supplier = st.selectbox('Supplier', ['All suppliers'] + suppliers, key='mapping_review_supplier')
    selected_records = records if supplier == 'All suppliers' else [
        r for r in records if (r.get('supplier_name') or '(Unknown supplier)') == supplier]
    rows, skipped = build_review_queue(selected_records, mapping)
    a, b, c = st.columns(3)
    a.metric('Distinct fields awaiting mapping', len(rows))
    b.metric('Header fields', sum(r['Section'] == 'header' for r in rows))
    c.metric('Line fields', sum(r['Section'] == 'lines' for r in rows))
    st.caption('Invoice counts are per field; an invoice may contain several fields. '
               'Configured mappings and intentional exclusions are removed from this queue.')
    if skipped:
        st.warning(f'{skipped} invoice audit(s) could not be used; counts cover readable coverage audits only.')
    if not rows:
        st.info('No outstanding fields found in the readable audits for this selection.')
        return
    frame = pd.DataFrame(rows)
    st.dataframe(frame, hide_index=True, width='stretch')
    st.download_button('Download review queue (CSV)', frame.to_csv(index=False).encode('utf-8-sig'),
                       'ocr_mapping_review.csv', 'text/csv')
    selected = st.selectbox('Field to action', range(len(rows)),
                            format_func=lambda i: rows[i]['Section'] + ' / ' + rows[i]['OCR field'],
                            key='mapping_review_field')
    row = rows[selected]
    st.caption('Actions below apply globally to this tag. For supplier-specific decisions, '
               'use Supplier rules on the Edit mapping tab. Existing invoices are not rewritten.')
    with st.form('mapping_review_action'):
        action = st.radio('Decision', ['Map to BC field', 'Intentionally exclude'], horizontal=True)
        target = st.text_input('Exact BC field caption (required when mapping)')
        value_type = st.selectbox('Value type', FIELD_TYPES)
        submitted = st.form_submit_button('Save decision', type='primary')
    if submitted:
        target = target.strip() if action == 'Map to BC field' else 'Z_' + row['OCR field']
        if not target:
            st.error('Enter the exact Business Central field caption.')
            return
        try:
            # Reload to avoid replacing changes saved after the queue was rendered.
            latest = load_field_mapping()
            if any(row['OCR field'] in e['docmateTags'] for e in latest[row['Section']]):
                raise ValueError('This tag is already configured. Refresh and review the existing mapping.')
            updated = add_review_mapping(latest, row['Section'], row['OCR field'], target, value_type)
            save_field_mapping(updated)
        except Exception as exc:
            st.error(f'Decision could not be saved: {exc}')
            return
        for key in ('bc_map_header', 'bc_map_lines', 'bc_map_rules', 'bc_map_settings',
                    'bc_map_max_vat_diff', 'bc_map_vat_decimals'):
            st.session_state.pop(key, None)
        st.session_state['mapping_review_saved'] = True
        st.rerun()

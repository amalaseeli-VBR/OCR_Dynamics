"""Admin page where support maintains DocMate tag -> BC field captions without code changes."""
import json
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from .bc_table_mapping import (
    DEFAULT_FIELD_MAPPING, FIELD_TYPES, MAPPING_FILE, SECTIONS, generate_al_snippet,
    load_field_mapping, map_bc_tables, save_field_mapping, validate_field_mapping,
)

SAMPLE_INVOICE = {
    'header': {
        'supplierName': 'Booker Ltd', 'customerName': 'The Company', 'invoiceNumber': 'INV-1001',
        'invoiceDate': '2026-10-01', 'dueDate': '2026-10-31', 'orderNumber': 'SO-5521',
        'accountNumber': 'ACC-778', 'customerPONumber': 'PO-3301', 'currency': 'GBP',
        'vatRegistrationNumber': 'GB123456789', 'totalNet': 108.0, 'totalVat': 21.6, 'totalGross': 129.6,
    },
    'items': [
        {'lineNo': 1, 'code': 'BK001', 'description': 'Cola 24x330ml', 'unitSize': 'CASE', 'qty': 2,
         'unitPrice': 60.0, 'discount': 10, 'lineNet': 108.0, 'vatRate': 20, 'vatAmount': 21.6,
         'lineGross': 129.6},
    ],
}


def _to_frame(entries):
    return pd.DataFrame([{'BC field': e['bcField'], 'DocMate tags': ', '.join(e['docmateTags']),
                          'Type': e['type']} for e in entries])


def _from_frame(frame):
    entries = []
    for row in frame.to_dict('records'):
        field = str(row.get('BC field') or '').strip()
        tags = [t.strip() for t in str(row.get('DocMate tags') or '').split(',') if t.strip()]
        if not field and not tags:
            continue  # blank row added in the editor
        entries.append({'bcField': field, 'docmateTags': tags, 'type': row.get('Type') or 'text'})
    return entries


def _editor(entries, key):
    return st.data_editor(
        _to_frame(entries), key=key, num_rows='dynamic', hide_index=True, width='stretch',
        column_config={
            'BC field': st.column_config.TextColumn(
                help='Exact Business Central field caption the BC code reads, e.g. Vendor Invoice No.',
                required=True),
            'DocMate tags': st.column_config.TextColumn(
                help='DocMate extraction tags, comma separated. The first one with a value is used.',
                required=True),
            'Type': st.column_config.SelectboxColumn(options=list(FIELD_TYPES), required=True,
                                                     help='decimal/integer values are checked before sending.'),
        },
    )


def _rules_editor(rules, field_names):
    frame = pd.DataFrame([{'Supplier': r['supplier'], 'Section': r['section'], 'DocMate tag': r['docmateTag'],
                           'BC field': r['bcField']} for r in rules],
                         columns=['Supplier', 'Section', 'DocMate tag', 'BC field'])
    edited = st.data_editor(
        frame, key='bc_map_rules', num_rows='dynamic', hide_index=True, width='stretch',
        column_config={
            'Supplier': st.column_config.TextColumn(
                help='Part of the supplier name as DocMate reads it, e.g. Booker matches "Booker Ltd".'),
            'Section': st.column_config.SelectboxColumn(options=list(SECTIONS)),
            'DocMate tag': st.column_config.TextColumn(help='The DocMate tag to treat differently, e.g. discount'),
            'BC field': st.column_config.SelectboxColumn(options=field_names),
        },
    )
    return [{'supplier': str(r.get('Supplier') or '').strip(), 'section': r.get('Section') or 'lines',
             'docmateTag': str(r.get('DocMate tag') or '').strip(), 'bcField': str(r.get('BC field') or '').strip()}
            for r in edited.to_dict('records')
            if any(str(r.get(c) or '').strip() for c in ('Supplier', 'DocMate tag', 'BC field'))]


def _settings_editor(settings):
    frame = pd.DataFrame([{'Supplier': s['supplier'], 'Prices Including VAT': s['pricesIncludingVat']}
                          for s in settings], columns=['Supplier', 'Prices Including VAT'])
    frame['Prices Including VAT'] = frame['Prices Including VAT'].astype(bool)
    edited = st.data_editor(
        frame, key='bc_map_settings', num_rows='dynamic', hide_index=True, width='stretch',
        column_config={
            'Supplier': st.column_config.TextColumn(help='Part of the supplier name, e.g. Booker.'),
            'Prices Including VAT': st.column_config.CheckboxColumn(
                help='Ticked: this supplier prints unit prices including VAT.', default=False),
        },
    )
    return [{'supplier': str(r.get('Supplier') or '').strip(), 'pricesIncludingVat': bool(r.get('Prices Including VAT'))}
            for r in edited.to_dict('records') if str(r.get('Supplier') or '').strip()]


def _raw_tags(record):
    """Keys sent under their DocMate tag (no BC tag), excluding the additionalFields block."""
    return [k for k in record if k != 'additionalFields' and not (' ' in k or k[:1].isupper())]


def render_bc_field_mapping():
    st.header('BC Field Mapping')
    st.caption('Maps DocMate extraction tags to the Business Central field captions sent in the Dynamics JSON '
               '(Purchase Header 38 / Purchase Line 39). Changes apply to the next invoice processed; '
               'no restart or code change is needed.')
    if not st.session_state.get('is_admin'):
        st.warning('BC Field Mapping is Admin-only.')
        return
    try:
        current = load_field_mapping()
    except Exception as exc:
        st.error(f'The saved mapping file cannot be used: {exc}')
        st.caption(f'File: {MAPPING_FILE}. Fix it below and save, or reset to defaults.')
        current = load_field_mapping(MAPPING_FILE.with_name('__missing__.json'))

    review_tab, edit_tab, test_tab, capture_tab, al_tab = st.tabs(
        ['Fields awaiting mapping', 'Edit mapping', 'Test with an invoice',
         'PDF data capture', 'BC (AL) code'])
    with review_tab:
        from .bc_mapping_review import render_mapping_review
        if st.session_state.pop('mapping_review_saved', False):
            st.success('Mapping decision saved. It applies to subsequent invoices.')
        render_mapping_review(current)
    with edit_tab:
        st.caption('Z_ mapping codes intentionally exclude a tag from BC writes. '
                   'Excluded values remain in the invoice mapping audit. '
                   'Vendor-specific rules take priority over default mappings.')
        st.subheader('Invoice header')
        header_frame = _editor(current['header'], 'bc_map_header')
        st.subheader('Invoice lines')
        lines_frame = _editor(current['lines'], 'bc_map_lines')
        st.subheader('Supplier rules')
        st.caption('Use when a supplier prints the same column with a different meaning. Example: supplier '
                   '"Booker", lines, tag "discount", BC field "Line Discount %". A plain discount from a '
                   'supplier without a rule is held for review, never sent with a guess.')
        field_names = sorted({e['bcField'] for s in SECTIONS for e in current[s]}
                             | {e['bcField'] for s in SECTIONS for e in _from_frame(
                                 header_frame if s == 'header' else lines_frame)})
        rules = _rules_editor(current.get('supplierRules', []), field_names)
        st.subheader('Supplier settings')
        st.caption('Tick "Prices Including VAT" for suppliers whose unit prices include VAT. DocMate sends '
                   'Prices Including VAT to BC and checks each line against the right total (gross or net). '
                   'Suppliers not listed use the BC vendor default and are checked as VAT-exclusive.')
        settings = _settings_editor(current.get('supplierSettings', []))
        st.subheader('VAT check')
        st.caption('Line VAT has no BC tag: BC calculates it. DocMate recalculates VAT per tax band (lines rounded '
                   'to the decimals below, band totals to 2) and compares it with the invoice VAT. A difference '
                   'up to the allowed amount is sent as the VAT difference; more is held for review.')
        vat_col1, vat_col2 = st.columns(2)
        with vat_col1:
            max_diff = st.number_input('Max VAT difference allowed (£)', min_value=0.0, max_value=1.0, step=0.01,
                                       value=float(current['vatRules']['maxVatDifference']), format='%.2f',
                                       key='bc_map_max_vat_diff')
        with vat_col2:
            decimals = st.selectbox('Line VAT rounding (decimals)', [5, 4, 3, 2],
                                    index=[5, 4, 3, 2].index(current['vatRules']['lineRoundingDecimals']),
                                    key='bc_map_vat_decimals')
        edited = {'header': _from_frame(header_frame), 'lines': _from_frame(lines_frame), 'supplierRules': rules,
                  'supplierSettings': settings,
                  'vatRules': {'maxVatDifference': max_diff, 'lineRoundingDecimals': decimals}}
        errors = validate_field_mapping(edited)
        for error in errors:
            st.error(error)
        save_col, reset_col = st.columns(2)
        with save_col:
            if st.button('Save mapping', type='primary', disabled=bool(errors), width='stretch'):
                save_field_mapping(edited)
                st.success('Mapping saved. The previous version was kept as bc_field_mapping.json.bak.')
        with reset_col:
            if st.button('Reset to DocMate defaults', width='stretch'):
                save_field_mapping(DEFAULT_FIELD_MAPPING)
                for key in ('bc_map_header', 'bc_map_lines', 'bc_map_rules', 'bc_map_settings',
                            'bc_map_max_vat_diff', 'bc_map_vat_decimals'):
                    st.session_state.pop(key, None)
                st.rerun()
        preview_mapping = edited if not errors else current

    with test_tab:
        st.caption('Shows the JSON BC will receive for an invoice, using the mapping on the Edit tab '
                   '(including unsaved changes). Upload an extracted invoice JSON or use the sample.')
        upload = st.file_uploader('Extracted invoice JSON ({"header": {...}, "items": [...]})', type=['json'],
                                  key='bc_map_test_upload')
        pdf_text = st.text_area('PDF text (optional): paste the invoice text to see which values DocMate '
                                'calculated (sent in additionalFields)', key='bc_map_test_text', height=120)
        invoice = SAMPLE_INVOICE
        if upload is not None:
            try:
                invoice = json.loads(upload.getvalue().decode('utf-8-sig'))
                if not isinstance(invoice.get('header'), dict):
                    raise ValueError('JSON needs a "header" object.')
            except Exception as exc:
                st.error(f'Cannot read this JSON: {exc}')
                invoice = SAMPLE_INVOICE
        header = invoice['header']
        items = invoice.get('items') or invoice.get('lines') or invoice.get('lineItems') or []
        try:
            outgoing = map_bc_tables(header, items, {'buffer_header': {}, 'buffer_lines': []}, preview_mapping,
                                     source_text=pdf_text or None)
        except ValueError as exc:
            st.error(str(exc))
        else:
            left, right = st.columns(2)
            with left:
                st.subheader('DocMate extracted')
                st.json(invoice)
            with right:
                st.subheader('Sent to BC')
                st.json({'invoiceHeader': outgoing['invoiceHeader'], 'invoiceLines': outgoing['invoiceLines'],
                         'vatSummary': outgoing['vatSummary'], 'pdfCheck': outgoing['pdfCheck']})
            if outgoing['supplierRulesApplied']:
                st.info('Supplier rules used: ' + ', '.join(outgoing['supplierRulesApplied']))
            for reason in outgoing['reviewReasons']:
                st.error('Held for review: ' + reason)
            st.caption('Every extracted value is sent: under its BC tag when mapped, otherwise under its DocMate '
                       'tag. Values DocMate calculated (not printed on the PDF) are in "additionalFields". '
                       + outgoing['pdfCheck']['note'])
            raw = _raw_tags(outgoing['invoiceHeader']) + sorted(
                {f'line.{k}' for line in outgoing['invoiceLines'] for k in _raw_tags(line)})
            if raw:
                st.info('Sent with their DocMate tag (no BC tag yet): ' + ', '.join(raw))
            else:
                st.success('Every extracted value has a BC tag.')

    with capture_tab:
        _render_capture_test()

    with al_tab:
        st.caption('Reader code for the BC developer, generated from the mapping on the Edit tab. '
                   'Copy it after any mapping change so BC reads the same captions DocMate sends.')
        st.code(generate_al_snippet(preview_mapping), language='pascal')


def _render_capture_test():
    st.caption('Upload an invoice to see everything DocMate captures from it and sends to BC as "pdfData": '
               'every "label: value" printed on it (labels as printed, any language), address blocks, '
               'tables cell by cell, and the full text of each page. Text drawn as images or shapes is read '
               'with OCR on this computer.')
    upload = st.file_uploader('Invoice (PDF or image)', type=['pdf', 'png', 'jpg', 'jpeg', 'tif', 'tiff'],
                              key='bc_map_capture_upload')
    use_ocr = st.checkbox('Use OCR for scanned pages and text drawn as images', value=True, key='bc_map_capture_ocr')
    use_gemini = st.checkbox('Also analyse with Gemini (sends the document to Google Gemini)', value=False,
                             key='bc_map_capture_gemini')
    if upload is None or not st.button('Capture data', type='primary', key='bc_map_capture_run'):
        return
    from .pdf_capture import capture_document
    from .gemini_capture import gemini_capture, gemini_settings
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / Path(upload.name).name
        path.write_bytes(upload.getvalue())
        with st.spinner('Reading every page...'):
            try:
                data = capture_document(path, use_ocr=use_ocr)
            except Exception as exc:
                st.error(f'Could not read this file: {exc}')
                return
        if use_gemini:
            cfg = gemini_settings()
            with st.spinner(f"Gemini ({cfg['model']}) is reading the document..."):
                try:
                    data['gemini'] = {'ran': True, 'reason': 'requested', **gemini_capture(path, cfg['api_key'], cfg['model'])}
                except Exception as exc:
                    data['gemini'] = {'ran': False, 'error': f'Gemini capture failed: {exc}'}
    st.success(f"{data['pagesRead']} of {data['pageCount']} page(s) read, OCR on {data['ocrPages']}, "
               f"language: {data['language'] or 'unknown'}. {len(data['fields'])} fields, "
               f"{len(data['tables'])} table(s).")
    st.subheader('Fields')
    st.dataframe(pd.DataFrame([{'Label (as printed)': k, 'Value': v} for k, v in data['fields'].items()]),
                 hide_index=True, width='stretch')
    for table in data['tables']:
        st.subheader(f"Table on page {table['page']}")
        st.dataframe(pd.DataFrame(table['rows'], columns=table['columns']), hide_index=True, width='stretch')
    with st.expander('Full text of each page'):
        for page in data['pages']:
            st.text(f"--- Page {page['page']} ---\n{page['text']}")
    gemini = data.get('gemini')
    if gemini:
        st.subheader(f"Gemini analysis ({gemini.get('model', '')})")
        if gemini.get('error'):
            st.error(gemini['error'])
        else:
            if gemini.get('note'):
                st.warning(gemini['note'])
            st.dataframe(pd.DataFrame([{'Label (as printed)': k, 'Value': v} for k, v in gemini['fields'].items()]),
                         hide_index=True, width='stretch')
            for address in gemini['addresses']:
                st.text(f"{address['role']} - {address['label'] or '(no label)'}\n{address['text']}")
            for table in gemini['tables']:
                st.dataframe(pd.DataFrame(table['rows'], columns=table['columns'] or None),
                             hide_index=True, width='stretch')
            if gemini['notes']:
                with st.expander('Other printed text (Gemini)'):
                    st.text('\n\n'.join(gemini['notes']))
    st.download_button('Download pdfData JSON', json.dumps(data, indent=2, ensure_ascii=False),
                       f'{Path(upload.name).stem}_pdfData.json', 'application/json')

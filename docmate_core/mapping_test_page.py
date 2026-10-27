"""Dynamics mapping test page, shared by existing Streamlit entrypoints."""
import json

import streamlit as st

from .mapping_test import extract_invoice, map_invoice


def render_mapping_test(app, settings):
    st.header('Dynamics Mapping Test')
    st.caption('Upload several invoices to compare extracted data with Business Central mapping. '
               'Preview only: invoice records are not saved to SQL or sent to Dynamics.')
    st.info('Use one invoice per file. PDF text is read first; scanned PDFs and images use your OCR settings. '
            'You can also upload extracted invoice JSON to test mapping directly.')
    uploads = st.file_uploader('Upload invoices', type=['pdf', 'png', 'jpg', 'jpeg', 'json'],
                               accept_multiple_files=True, key='mapping_uploads')
    force_ocr = st.checkbox('Force OCR for PDFs', key='mapping_force_ocr')
    engine = st.selectbox('OCR engine for scanned invoices', ['doctr', 'tesseract', 'easyocr'],
                          key='mapping_ocr_engine')
    max_pages = st.number_input('Maximum pages per invoice', min_value=1, max_value=100,
                                value=max(1, min(100, int(settings.get('ocr_max_pages', 10) or 10))),
                                key='mapping_max_pages')
    with st.expander('BC vendor, G/L account and dimensions (optional)'):
        st.caption('Enter your customer mapping configuration. Supplier names should be lowercase. '
                   'These settings apply only to this test session.')
        config_text = st.text_area('Customer mapping JSON', value='{"suppliers": {}, "defaults": {}}',
                                   height=180, key='mapping_customer_config')
        st.code('''{"suppliers": {"booker wholesale": {"bcVendorNo": "V10042",
  "bcGlAccount": "6110", "dimensions": {"STORE": "ST-01"}}}}''', language='json')
    if st.button('Extract and test mapping', type='primary', disabled=not uploads):
        try:
            config = json.loads(config_text)
            if not isinstance(config, dict):
                raise ValueError('Customer mapping must be a JSON object.')
            for key in ('suppliers', 'defaults'):
                if not isinstance(config.get(key, {}), dict):
                    raise ValueError(f'{key} must be a JSON object.')
            if any(not isinstance(value, dict) for value in config.get('suppliers', {}).values()):
                raise ValueError('Each supplier configuration must be a JSON object.')
        except (ValueError, TypeError) as exc:
            st.error(f'Invalid customer mapping: {exc}')
        else:
            results = []
            progress = st.progress(0)
            for index, upload in enumerate(uploads):
                result = {'file': upload.name}
                with st.spinner(f'Processing {index + 1}/{len(uploads)}: {upload.name}'):
                    try:
                        run_settings = dict(settings, ocr_engine=engine, ocr_max_pages=int(max_pages))
                        raw, text, source = extract_invoice(upload.name, upload.getvalue(), app, run_settings, force_ocr)
                        result.update(raw=raw, text=text, extraction=source)
                        result.update(map_invoice(raw, config))
                    except Exception as exc:
                        result['error'] = str(exc)
                results.append(result)
                progress.progress((index + 1) / len(uploads))
            st.session_state['mapping_results'] = results
    results = st.session_state.get('mapping_results', [])
    if not results:
        return
    st.subheader('Latest test results')
    st.caption('Results below belong to the last run. Run the test again after changing files or settings.')
    st.dataframe([{'File': r['file'], 'Status': 'Failed' if r.get('error') else
                   ('Review' if r['warnings'] else 'Mapped'),
                   'Invoice': r.get('buffer_header', {}).get('InvoiceNumber', ''),
                   'Lines': len(r.get('buffer_lines', []))} for r in results], use_container_width=True)
    st.download_button('Download all results (JSON)', json.dumps(results, indent=2, default=str),
                       'dynamics_mapping_results.json', 'application/json')
    for index, result in enumerate(results):
        with st.expander(f'{index + 1}. {result["file"]}', expanded=len(results) == 1):
            if result.get('error'):
                st.error(result['error'])
                if 'raw' in result:
                    st.json(result['raw'])
                continue
            for warning in result['warnings']:
                st.warning(warning)
            comparison, canonical, text_tab = st.tabs(['Mapping view', 'Canonical JSON', 'Extracted text'])
            with comparison:
                left, right = st.columns(2)
                with left:
                    st.subheader('Extracted invoice')
                    st.caption(result['extraction'])
                    st.json(result['raw'])
                with right:
                    st.subheader('Dynamics header')
                    st.dataframe([{'Field': key, 'Value': str(value) if value is not None else ''}
                                  for key, value in result['buffer_header'].items()], use_container_width=True)
                    st.subheader('Dynamics lines')
                    st.dataframe(result['buffer_lines'], use_container_width=True)
            with canonical:
                st.json(result['canonical'])
            with text_tab:
                st.text(result['text'] or 'JSON upload: OCR was not required.')
    if st.button('Clear test results'):
        st.session_state.pop('mapping_results', None)
        st.rerun()

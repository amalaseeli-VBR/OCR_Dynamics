"""Read-only extraction and BC mapping preview helpers."""
import dataclasses
import hashlib
import json

from .invoice_mapping import mapping_input, map_invoice
from .convelio_parser import parse_convelio


def extract_invoice(name, content, app, settings, force_ocr=False):
    """Reuse the running app's OCR and invoice parser, without persistence."""
    if name.lower().endswith('.json'):
        raw = json.loads(content.decode('utf-8-sig'))
        if not isinstance(raw, dict) or not raw:
            raise ValueError('Upload a JSON object containing one extracted invoice.')
        return raw, '', 'Uploaded JSON'
    mime = app['_guess_mime_type'](name, content)
    text = ''
    source = 'PDF text'
    if mime == 'application/pdf' and not force_ocr:
        text = app['_pdf_text_layer'](content, max_pages=int(settings.get('ocr_max_pages', 10)))
    if not text.strip():
        source = 'OCR'
        cfg = app['OcrConfig'](
            engine=settings.get('ocr_engine') or 'doctr',
            tesseract_cmd=settings.get('tesseract_cmd', ''),
            dpi=int(settings.get('ocr_dpi', 250)),
            max_pages=int(settings.get('ocr_max_pages', 10)),
            force_pdf=force_ocr, auto_install=False,
        )
        key = hashlib.sha256(content).hexdigest() + '|mapping|' + str(cfg)
        text = app['ocr_text_cached'](key, mime, dataclasses.asdict(cfg), content,
                                      progress_cb=None, progress_label=name)
    if not (text or '').strip():
        raise ValueError('No invoice text was extracted. Check OCR settings or try another file.')
    parsed = None
    for selector in ('_select_dhamecha_authority', '_select_supplier_authority'):
        if selector in app:
            authority = (app[selector]((text,)) if selector == '_select_dhamecha_authority'
                         else app[selector]((text,), ()))
            if authority:
                parsed = {'header': authority[0], 'items': authority[1]}
                break
    if parsed is None:
        parsed = parse_convelio(text)
    if parsed is None:
        parsed = app['LocalInvoiceParser'].parse(text, 'Purchase Invoice')
    return parsed, text, source


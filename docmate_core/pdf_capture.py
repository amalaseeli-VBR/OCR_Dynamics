"""Capture every piece of data printed on an invoice, whatever the supplier or language.

The invoice extractor only returns the fields it knows. This module reads the whole
document instead, so nothing printed is lost:

1. Words from the PDF text layer, plus OCR (docTR, local) for text the text layer does not
   contain: scanned pages, images, and text drawn as vector shapes.
2. Words are rebuilt into rows and column segments from their positions.
3. Generic capture using the labels as printed (works for any language):
   - tables: a header row and the rows under it, cell by cell (ITEM | DISCOUNT | TOTAL)
   - "Label: value" (Hub ID: 5375442), including multi-line values (Company address: ...)
   - a row of labels with the values underneath (Invoice date | Due date | Invoice no.)
   - unlabelled blocks of stacked lines, e.g. a customer address printed without a heading
   - a label with its value to the right or underneath (Due Date / June 29, 2022)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from statistics import median

logger = logging.getLogger(__name__)

MAX_PAGES = 20
OCR_DPI = 200
_IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp'}
_DIGIT = re.compile(r'\d')
_LETTER = re.compile(r'[^\W\d_]', re.UNICODE)
_PERCENT = re.compile(r'\d+(?:[.,]\d+)?\s?%')
_AMOUNT = re.compile(r'^\W{0,3}[A-Za-z]{0,3}\s?-?\d[\d\s.,]*%?\s?[A-Za-z€£$]{0,4}-?$')
_MONEY = re.compile(r'\d[.,]\d{2}\b')
_MAX_LABEL_GAP = 0.22  # a value further right than this belongs to something else
_OCR_MODEL = None


# ---- reading words ------------------------------------------------------------------

def _ocr_model():
    global _OCR_MODEL
    if _OCR_MODEL is None:
        from doctr.models import ocr_predictor  # local models, nothing is uploaded
        _OCR_MODEL = ocr_predictor(pretrained=True)
    return _OCR_MODEL


def _ocr_words(image):
    """OCR one page image; words with boxes normalised to 0..1."""
    result = _ocr_model()([image])
    words = []
    for block in result.pages[0].blocks:
        for line in block.lines:
            for word in line.words:
                (x0, y0), (x1, y1) = word.geometry
                words.append({'x0': x0, 'y0': y0, 'x1': x1, 'y1': y1, 'text': word.value, 'ocr': True})
    return words


def _page_image(page):
    import numpy as np
    pix = page.get_pixmap(dpi=OCR_DPI)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)[:, :, :3].copy()


def _covered(word, others):
    cx, cy = (word['x0'] + word['x1']) / 2, (word['y0'] + word['y1']) / 2
    return any(o['x0'] - 0.005 <= cx <= o['x1'] + 0.005 and o['y0'] - 0.005 <= cy <= o['y1'] + 0.005
               for o in others)


def _needs_ocr(page, text_words):
    """OCR when the page may show text the text layer lacks: a scan, a large image, or text drawn
    as vector shapes. A small logo alone does not need OCR."""
    if len(text_words) < 15:
        return True
    area = page.rect.width * page.rect.height or 1
    images = sum(abs(pymupdf_rect_area(info.get('bbox'))) for info in page.get_image_info())
    return images / area >= 0.08 or len(page.get_drawings()) > 10


def pymupdf_rect_area(bbox):
    if not bbox:
        return 0.0
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _pdf_page_words(page, use_ocr):
    w, h = page.rect.width, page.rect.height
    words = [{'x0': x0 / w, 'y0': y0 / h, 'x1': x1 / w, 'y1': y1 / h, 'text': t, 'ocr': False}
             for x0, y0, x1, y1, t, *_ in page.get_text('words') if t.strip()]
    if use_ocr and _needs_ocr(page, words):
        try:
            words += [o for o in _ocr_words(_page_image(page)) if not _covered(o, words)]
        except Exception as exc:  # OCR is a bonus: never lose the text layer because of it
            logger.warning('PDF capture OCR failed on page %s: %s', page.number + 1, exc)
    return words


# ---- rows and segments ----------------------------------------------------------------

def _rows(words):
    """Group words into visual rows, each split into column segments by large gaps."""
    if not words:
        return []
    height = median(w['y1'] - w['y0'] for w in words) or 0.01
    rows = []
    for word in sorted(words, key=lambda w: ((w['y0'] + w['y1']) / 2, w['x0'])):
        yc = (word['y0'] + word['y1']) / 2
        if rows and abs(yc - rows[-1]['yc']) <= height * 0.55:
            rows[-1]['words'].append(word)
            rows[-1]['yc'] = (rows[-1]['yc'] * (len(rows[-1]['words']) - 1) + yc) / len(rows[-1]['words'])
        else:
            rows.append({'yc': yc, 'words': [word]})
    result = []
    for row in rows:
        segments = []
        for word in sorted(row['words'], key=lambda w: w['x0']):
            if segments and word['x0'] - segments[-1]['x1'] <= height * 1.6:
                segments[-1]['text'] += ' ' + word['text']
                segments[-1]['x1'] = max(segments[-1]['x1'], word['x1'])
                segments[-1]['words'].append(word)
            else:
                segments.append({'text': word['text'], 'x0': word['x0'], 'x1': word['x1'], 'words': [word]})
        for segment in segments:
            segment['text'] = segment['text'].strip()
        result.append({'y': row['yc'], 'segments': segments, 'height': height})
    return result


# ---- text tests -------------------------------------------------------------------------

def _has_digit(text):
    return bool(_DIGIT.search(text))


def _amount_like(text):
    """'£2,071.17', '20%', '12.00 EUR', '5.00-' - a money, quantity or percent value."""
    return bool(_AMOUNT.match(text.strip()))


def _value_like(text):
    """A value worth pairing with a label above it: an amount, a date or a reference."""
    return _amount_like(text) or len(_DIGIT.findall(text)) >= 2


def _label_like(text):
    """Short text made of words: 'Due Date', 'Total Due (GBP)', 'Uw BTW nummer', 'TVA 20%'."""
    text = text.strip().rstrip(':').strip()
    words = text.split()
    bare = _PERCENT.sub('', re.sub(r'\(.*?\)', '', text))
    return 1 <= len(words) <= 6 and len(text) <= 45 and bool(_LETTER.search(text)) and not _has_digit(bare)


def _colon_pair(text):
    """'Hub ID: 5375442' -> ('Hub ID', '5375442'); ignores times and web addresses."""
    match = re.match(r'^\s*([^:]{2,45}?)\s*:\s*(.+)$', text)
    if not match:
        return None
    label, value = match.group(1).strip(), match.group(2).strip()
    if label.lower().endswith(('http', 'https', 'www')) or not _LETTER.search(label) or _has_digit(label[-1:]):
        return None
    return (label, value) if _label_like(label) else None


def _looks_value(text):
    """A value rather than a heading: has numbers, an e-mail or web address, or is a long phrase."""
    return _value_like(text) or '@' in text or text.lower().startswith('www') or not _label_like(text)


def _is_label(segment):
    return segment['text'].endswith(':') or bool(_colon_pair(segment['text']))


def _same_column(a, b, tolerance=0.025):
    return abs(a['x0'] - b['x0']) <= tolerance


def _value_anchor(segment):
    """Left edge of the value in 'Label: value' (the first word after the colon)."""
    words = segment.get('words') or []
    for index, word in enumerate(words):
        if ':' in word['text']:
            rest = word['text'].split(':', 1)[1]
            if rest.strip() or index + 1 >= len(words):
                return {'x0': word['x0']}
            return {'x0': words[index + 1]['x0']}
    return {'x0': segment['x0']}


# ---- tables ---------------------------------------------------------------------------------

def _is_header(row):
    segments = row['segments']
    words = [s for s in segments if _label_like(s['text']) and ':' not in s['text']]
    symbols = [s for s in segments if not _LETTER.search(s['text']) and len(s['text']) <= 2]
    return (len(words) >= 3 and len(words) + len(symbols) == len(segments) and len(symbols) <= 1
            and sum(len(s['text']) for s in segments) < 200)


def _column_of(segment, columns):
    centre = (segment['x0'] + segment['x1']) / 2
    for column in columns:
        if column['left'] <= centre < column['right']:
            return column['name']
    return min(columns, key=lambda c: abs((c['x0'] + c['x1']) / 2 - centre))['name']


def _capture_tables(rows, page_no):
    tables, table_rows, index = [], set(), 0
    while index < len(rows):
        header = rows[index]
        ahead = [s for r in rows[index + 1:index + 5] for s in r['segments']]
        if not _is_header(header) or not any(_amount_like(s['text']) for s in ahead):
            index += 1
            continue
        segs = header['segments']
        columns = []
        for position, seg in enumerate(segs):
            left = 0.0 if position == 0 else (segs[position - 1]['x1'] + seg['x0']) / 2
            right = 1.0 if position == len(segs) - 1 else (seg['x1'] + segs[position + 1]['x0']) / 2
            name = seg['text']
            if any(c['name'] == name for c in columns):
                name = f'{name} ({position + 1})'
            columns.append({'name': name, 'left': left, 'right': right, 'x0': seg['x0'], 'x1': seg['x1']})
        first = columns[0]['name']
        body, last_y, current, current_amounts, rows_used = [], header['y'], None, set(), []
        cursor = index + 1
        while cursor < len(rows):
            row = rows[cursor]
            if row['y'] - last_y > row['height'] * 4.5 or _is_header(row):
                break
            cells = {}
            for seg in row['segments']:
                name = _column_of(seg, columns)
                cells[name] = (cells[name] + ' ' + seg['text']) if name in cells else seg['text']
            amounts = {name for name, value in cells.items() if _amount_like(value)}
            if first not in cells and amounts and any(
                    _label_like(v) and not _amount_like(v) for v in cells.values()):
                break  # 'Subtotal  £2,071.17' under the table: a totals line, not an item
            if not amounts and current_amounts and row['y'] - last_y > row['height'] * 3.2:
                break  # text further down without amounts is after the table, not part of an item
            if current is not None and not (amounts & current_amounts):
                for name, value in cells.items():  # wrapped text continuing the item
                    current[name] = (current[name] + '\n' + value) if name in current else value
                current_amounts |= amounts
            else:  # a new item (its description may come before its amounts)
                current, current_amounts = dict(cells), set(amounts)
                body.append(current)
            rows_used.append(cursor)
            last_y = row['y']
            cursor += 1
        body = [r for r in body if any(_amount_like(v) for v in r.values())]
        if body:
            table_rows.update([index] + rows_used)
            tables.append({'page': page_no, 'columns': [c['name'] for c in columns], 'rows': body})
            index = cursor
        else:
            index += 1
    return tables, table_rows


# ---- fields and blocks ------------------------------------------------------------------------

class _Page:
    def __init__(self, rows, table_rows, page_no):
        self.rows, self.table_rows, self.page_no = rows, table_rows, page_no
        self.used, self.fields, self.blocks = set(), [], []

    def free(self, segment):
        return id(segment) not in self.used

    def add(self, label, segments, value=None):
        label = label.strip().rstrip(':').strip()
        value = value if value is not None else '\n'.join(s['text'] for s in segments)
        self.used.update(id(s) for s in segments)
        if label and value.strip():
            self.fields.append({'label': label, 'value': value.strip(), 'page': self.page_no})

    def below(self, start, anchor, label_x0, max_lines=8):
        """Lines stacked under `anchor` (same left edge), e.g. the lines of an address. Stops at a gap,
        a table, another label, or a new label in the label column."""
        lines = []
        last_y = self.rows[start - 1]['y'] if start else 0
        for index in range(start, min(len(self.rows), start + 40)):
            row = self.rows[index]
            if index in self.table_rows or row['y'] - last_y > row['height'] * 2.6:
                break
            if label_x0 is not None and any(abs(s['x0'] - label_x0) <= 0.02 and s['x0'] < anchor['x0'] - 0.02
                                            for s in row['segments']):
                break  # the next label in the label column
            match = next((s for s in row['segments'] if _same_column(s, anchor) and self.free(s)), None)
            if match is None:
                continue
            if _is_label(match):
                break
            right = next((s for s in row['segments'] if s['x0'] > match['x1']), None)
            if right is not None and right['x0'] - match['x1'] <= _MAX_LABEL_GAP \
                    and _label_like(match['text']) and _looks_value(right['text']):
                break  # 'Email   name@example.com': a pair of its own, not the next line of the block
            lines.append(match)
            last_y = row['y']
            if len(lines) >= max_lines:
                break
        return lines


def _label_rows(page):
    """A row of 3+ labels ending with ':' with the values on the next row."""
    for index, row in enumerate(page.rows[:-1]):
        labels = [s for s in row['segments'] if s['text'].endswith(':') and _label_like(s['text'])]
        if len(labels) < 3 or index in page.table_rows:
            continue
        values = page.rows[index + 1]['segments']
        if not values or not all(_value_like(v['text']) for v in values):
            continue  # e.g. three headings each followed by a block of text
        for label in labels:
            centre = (label['x0'] + label['x1']) / 2
            value = next((v for v in values if page.free(v) and (_same_column(v, label, 0.03)
                          or v['x0'] <= centre <= v['x1'])), None)
            page.used.add(id(label))
            if value is not None and not _is_label(value):
                page.add(label['text'], [value])


def _colon_fields(page):
    for index, row in enumerate(page.rows):
        if index in page.table_rows:
            continue
        segments = row['segments']
        for position, segment in enumerate(segments):
            if not page.free(segment):
                continue
            text = segment['text']
            right = next((s for s in segments[position + 1:] if page.free(s)), None)
            pair = _colon_pair(text)
            if pair:
                page.used.add(id(segment))
                lines = [] if _has_digit(pair[1]) else page.below(index + 1, _value_anchor(segment), segment['x0'])
                page.add(pair[0], lines, '\n'.join([pair[1]] + [s['text'] for s in lines]))
                continue
            if not (text.endswith(':') and _label_like(text)):
                continue
            page.used.add(id(segment))
            if right is not None and right['x0'] - segment['x1'] <= _MAX_LABEL_GAP and not _is_label(right):
                page.add(text, [right] + page.below(index + 1, right, segment['x0']))
                continue
            lines = page.below(index + 1, segment, None)
            if not lines and index + 1 < len(page.rows):  # value starts further right on the next row
                limit = min(segment['x1'] + _MAX_LABEL_GAP, right['x0'] if right is not None else 1.0)
                start = next((s for s in page.rows[index + 1]['segments'] if page.free(s)
                              and segment['x0'] < s['x0'] < limit and not _is_label(s)), None)
                if start is not None:
                    lines = [start] + page.below(index + 2, start, segment['x0'])
            page.add(text, lines)


def _unlabelled_blocks(page):
    """3+ short lines stacked at the same left edge, e.g. an address printed without a heading."""
    for index, row in enumerate(page.rows):
        if index in page.table_rows:
            continue
        for segment in row['segments']:
            if not page.free(segment) or _is_label(segment) or len(segment['text']) > 60:
                continue
            lines, last_y = [segment], row['y']
            for nxt in range(index + 1, len(page.rows)):
                if nxt in page.table_rows or page.rows[nxt]['y'] - last_y > page.rows[nxt]['height'] * 1.9:
                    break
                match = next((s for s in page.rows[nxt]['segments'] if page.free(s) and
                              abs(s['x0'] - segment['x0']) <= 0.012 and not _is_label(s) and len(s['text']) <= 60),
                             None)
                if match is None:
                    break
                lines.append(match)
                last_y = page.rows[nxt]['y']
            if len(lines) >= 3:
                page.used.update(id(s) for s in lines)
                page.blocks.append({'page': page.page_no, 'text': '\n'.join(s['text'] for s in lines),
                                    'lines': lines})
    _zip_label_columns(page)
    for block in page.blocks:
        block.pop('lines', None)


def _zip_label_columns(page):
    """'Invoice No.' / 'Invoice Date' / ... beside 'GB-SI2601760' / '12 May 2026' / ... become fields."""
    for labels in list(page.blocks):
        names = labels.get('lines') or []
        if not names or not all(_label_like(s['text']) for s in names):
            continue
        for values in page.blocks:
            vals = values.get('lines') or []
            if values is labels or len(vals) != len(names) or vals[0]['x0'] <= names[0]['x1']:
                continue
            if all(abs(_row_of(page, n) - _row_of(page, v)) == 0 for n, v in zip(names, vals)):
                for name, value in zip(names, vals):
                    page.fields.append({'label': name['text'], 'value': value['text'], 'page': page.page_no})
                page.blocks = [b for b in page.blocks if b is not labels and b is not values]
                break


def _row_of(page, segment):
    return next(i for i, row in enumerate(page.rows) if any(s is segment for s in row['segments']))


def _pairs_on_row(page):
    """'Subtotal   £2,071.17', 'Totaal exclusief BTW   €   341,25', 'Bank   Barclays'."""
    for index, row in enumerate(page.rows):
        if index in page.table_rows:
            continue
        segments = row['segments']
        for position, segment in enumerate(segments):
            if not page.free(segment) or not _label_like(segment['text']):
                continue
            rest = [s for s in segments[position + 1:] if page.free(s)]
            close = rest and rest[0]['x0'] - segment['x1'] <= _MAX_LABEL_GAP and not _is_label(rest[0])
            joined = ' '.join(s['text'] for s in rest)
            if close and len(rest) == len(segments) - position - 1 and (
                    _amount_like(joined) or (len(segments) == 2 and position == 0)
                    or (len(rest) == 1 and _value_like(joined))):
                page.used.add(id(segment))
                page.add(segment['text'], rest, joined)  # value may span cells: '€ | 341,25'


def _pairs_stacked(page):
    """'Due Date' over 'June 29, 2022'."""
    for index, row in enumerate(page.rows):
        if index in page.table_rows:
            continue
        for segment in row['segments']:
            if not page.free(segment) or not _label_like(segment['text']):
                continue
            if index + 1 < len(page.rows) and index + 1 not in page.table_rows:
                below = next((s for s in page.rows[index + 1]['segments']
                              if _same_column(s, segment) and page.free(s)), None)
                if below is not None and _value_like(below['text']) and not _is_label(below) \
                        and page.rows[index + 1]['y'] - row['y'] <= row['height'] * 2.6:
                    page.used.add(id(segment))
                    page.add(segment['text'], [below])


def _one_row_tables_as_fields(tables):
    """A header with a single row of values is really fields: 'Invoice date' -> '20/03/2026'."""
    fields, kept = [], []
    for table in tables:
        values = table['rows'][0].values() if len(table['rows']) == 1 else []
        if values and all('\n' not in v for v in values) and not any(_MONEY.search(v) for v in values):
            fields += [{'label': k, 'value': v, 'page': table['page']} for k, v in table['rows'][0].items()]
        else:
            kept.append(table)
    return fields, kept


# ---- document --------------------------------------------------------------------------------

def _page_text(rows):
    return '\n'.join('   '.join(s['text'] for s in row['segments']) for row in rows)


def _language(text):
    try:
        from langdetect import DetectorFactory, detect
        DetectorFactory.seed = 0
        return detect(text) if len(text) > 40 else ''
    except Exception:
        return ''


def _field_map(fields):
    """label -> value; repeated labels get ' (2)', ' (3)' unless the value is the same."""
    result = {}
    for field in fields:
        label, value = field['label'], field['value']
        if result.get(label) == value:
            continue
        key, n = label, 2
        while key in result and result[key] != value:
            key, n = f'{label} ({n})', n + 1
        result[key] = value
    return result


def capture_document(path, use_ocr=True, max_pages=MAX_PAGES):
    """All data printed on the document: fields, unlabelled blocks, tables and the text of each page."""
    import pymupdf

    path = Path(path)
    document = pymupdf.open(path) if path.suffix.lower() not in _IMAGE_TYPES else None
    if document is None:  # a photo or scanned image: OCR only
        import numpy as np
        from PIL import Image
        page_words = [_ocr_words(np.array(Image.open(path).convert('RGB')))] if use_ocr else [[]]
        ocr_pages = 1 if use_ocr else 0
    else:
        page_words = [_pdf_page_words(page, use_ocr) for page in list(document)[:max_pages]]
        ocr_pages = sum(any(w['ocr'] for w in words) for words in page_words)
    pages, fields, blocks, tables = [], [], [], []
    for page_no, words in enumerate(page_words, 1):
        rows = _rows(words)
        page_tables, table_rows = _capture_tables(rows, page_no)
        page = _Page(rows, table_rows, page_no)
        _label_rows(page)
        _colon_fields(page)
        _pairs_on_row(page)
        _unlabelled_blocks(page)
        _pairs_stacked(page)
        one_row, page_tables = _one_row_tables_as_fields(page_tables)
        fields += one_row + page.fields
        blocks += page.blocks
        tables += page_tables
        pages.append({'page': page_no, 'text': _page_text(rows)})
    return {
        'pageCount': len(document) if document is not None else 1,
        'pagesRead': len(pages),
        'ocrPages': ocr_pages,
        'language': _language('\n'.join(p['text'] for p in pages)),
        'fields': _field_map(fields),
        'blocks': blocks,
        'tables': tables,
        'pages': pages,
    }

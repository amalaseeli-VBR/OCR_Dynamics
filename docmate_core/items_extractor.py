from __future__ import annotations
import re
from typing import List, Dict, Any, Tuple, Optional

try:
    from .ocr.base import OcrResult, OcrWord, OcrLine
except Exception:
    OcrResult = None
    OcrWord = None
    OcrLine = None

# -------------------------
# Booker text-parser (adapted from DocMate large script)
# -------------------------

def _looks_like_booker_invoice(text: str) -> bool:
    if not text:
        return False
    up = str(text).upper()
    if ("INVOICE NUMBER" in up) and ("TOTAL ITEMS" in up) and (("RRP" in up) and ("POR" in up)):
        return True
    if ("INVOICE NUMBER" in up) and ("INVOICE TOTAL" in up) and ("RATE" in up) and ("NETT" in up) and ("VAT" in up):
        return True
    return False


class BookerCombinedRowParser:
    DEFAULT_VAT_MAP = {
        "B": 20.0,
        "A": 0.0,
        "Z": 0.0,
        "Z/A": 0.0,
        "ZA": 0.0,
        "R": 5.0,
        "S/A": 20.0,
        "SA": 20.0,
    }

    @staticmethod
    def _normalise_line(line: str) -> str:
        s = (line or "").replace("\t", " ").replace("£", " ").strip()
        s = re.sub(r"(\d)\s*\.\s*(\d{2})", r"\1.\2", s)
        s = s.replace("m1", "ml").replace("M1", "ML")
        s = re.sub(r"\s+", " ", s)
        s = s.replace(" Sk ", " SL ").replace(" Sh ", " SL ").replace(" Si ", " SL ").replace(" S1 ", " SL ")
        return s.strip()

    @staticmethod
    def _is_code(tok: str) -> bool:
        t = re.sub(r"[^0-9]+", "", (tok or ""))
        return t.isdigit() and 5 <= len(t) <= 7

    @staticmethod
    def _as_float(tok: str):
        if tok is None:
            return None
        t = str(tok).strip()
        if not t:
            return None
        if len(t) == 1:
            u = t.upper()
            if u in ("L", "I", "|"):
                return 1.0
            if u == "O":
                return 0.0
        t = t.replace(",", "").replace("%", "")
        if t.endswith("-") and len(t) > 1:
            t = "-" + t[:-1]
        t = re.sub(r"[^0-9.\-]+", "", t)
        if t in ("", "-", ".", "-."):
            return None
        try:
            return float(t)
        except Exception:
            return None

    @staticmethod
    def _norm_vat_code(tok: str) -> str:
        t = (tok or "").strip().upper().replace(" ", "")
        if t in ("ZA", "Z-A", "Z/A"):
            return "Z/A"
        return t

    @staticmethod
    def _looks_like_vat_code(tok: str) -> bool:
        t = BookerCombinedRowParser._norm_vat_code(tok)
        return bool(re.match(r"^(B|A|Z|Z/A|R)$", t))

    @staticmethod
    def _norm_flag(tok: str) -> str:
        t = (tok or "").strip().upper()
        if t in ("SH", "SK", "SI", "S1", "S/L"):
            return "SL"
        if t in ("SL", "P"):
            return t
        return ""

    @staticmethod
    def _extract_vat_map(text: str) -> Dict[str, float]:
        upper = (text or "").upper()
        upper = re.sub(r"(\d)\s*\.\s*(\d{2})", r"\1.\2", upper)
        vat_map = dict(BookerCombinedRowParser.DEFAULT_VAT_MAP)
        for m in re.finditer(r"^\s*([A-Z](?:/[A-Z])?)\s*:\s*(\d{1,2}(?:\.\d+)?)\b", upper, flags=re.MULTILINE):
            code = BookerCombinedRowParser._norm_vat_code(m.group(1))
            rate = BookerCombinedRowParser._as_float(m.group(2))
            if rate is not None:
                vat_map[code] = float(rate)
        return vat_map

    def parse(self, text: str) -> List[Dict[str, Any]]:
        txt = (text or "")
        vat_map = self._extract_vat_map(txt)
        lines = [self._normalise_line(ln) for ln in txt.splitlines() if (ln or "").strip()]
        items: List[Dict[str, Any]] = []

        def should_skip(ln_up: str) -> bool:
            if not ln_up:
                return True
            if any(k in ln_up for k in ("SUB-TOTAL", "TOTAL ITEMS", "TOTAL SAVINGS", "SAVING DETAILS", "PROMOTION", "PROMOTIONS", "RATE ", "INVOICE TOTAL", "TOTALS")):
                return True
            if ln_up.startswith("CODE ") or "CODE DESCRIPTION" in ln_up:
                return True
            return False

        for ln in lines:
            raw_line = ln
            up = ln.upper()
            if should_skip(up):
                continue

            toks = ln.split()
            if len(toks) < 7:
                continue
            if not self._is_code(toks[0]):
                continue

            code_val = re.sub(r"[^0-9]+", "", toks[0])

            vat_idx = None
            for i in range(len(toks) - 1, 0, -1):
                if self._looks_like_vat_code(toks[i]):
                    if any(self._as_float(x) is not None for x in toks[i + 1 : i + 3]) or any(self._as_float(x) is not None for x in toks[max(0, i - 3) : i]):
                        vat_idx = i
                        break
            if vat_idx is None:
                continue

            vat_code = self._norm_vat_code(toks[vat_idx])
            vat_rate = float(vat_map.get(vat_code, BookerCombinedRowParser.DEFAULT_VAT_MAP.get(vat_code, 0.0)))

            por = None
            for j in range(len(toks) - 1, vat_idx, -1):
                if "%" in toks[j]:
                    por = self._as_float(toks[j])
                    break
            if por is None:
                for j in range(len(toks) - 1, vat_idx, -1):
                    v = self._as_float(toks[j])
                    if v is not None:
                        por = float(v)
                        break
            std_rrp = None
            if vat_idx + 1 < len(toks):
                std_rrp = self._as_float(toks[vat_idx + 1])

            value_idx = None
            for j in range(vat_idx - 1, 1, -1):
                if self._as_float(toks[j]) is not None:
                    value_idx = j
                    break
            if value_idx is None:
                continue
            line_net = float(self._as_float(toks[value_idx]) or 0.0)

            flag = ""
            scan_left_from = value_idx - 1
            if scan_left_from >= 0:
                f = self._norm_flag(toks[scan_left_from])
                if f:
                    flag = f
                    scan_left_from = value_idx - 2

            unit_price = None
            unit_price_idx = None
            for j in range(scan_left_from, 1, -1):
                v = self._as_float(toks[j])
                if v is not None:
                    unit_price = float(v)
                    unit_price_idx = j
                    break
            if unit_price_idx is None:
                continue

            qty = None
            qty_idx = None
            for j in range(unit_price_idx - 1, 1, -1):
                v = self._as_float(toks[j])
                if v is not None:
                    qty = float(v)
                    qty_idx = j
                    break
            if qty_idx is None:
                continue

            if qty_idx - 2 < 1:
                continue
            case_pack = self._as_float(toks[qty_idx - 2])
            unit_size = toks[qty_idx - 1]
            if case_pack is None:
                continue
            case_pack = int(case_pack)

            # Heuristic: sometimes OCR swaps unit_size and case_pack tokens.
            # If unit_size is numeric and the case_pack token looks like a size/PM token, swap.
            try:
                unit_size_s = str(unit_size or "").strip()
                case_pack_tok = str(toks[qty_idx - 2] or "").strip()
                if re.fullmatch(r"\d+(?:\.\d+)?", unit_size_s) and re.search(r"(PM\d+|\d+\s*(ML|CL|L|G|KG))", case_pack_tok, flags=re.IGNORECASE):
                    unit_size = case_pack_tok
                    case_pack = int(float(unit_size_s))
            except Exception:
                pass

            desc_tokens = toks[1 : qty_idx - 2]
            if not desc_tokens:
                continue
            description = " ".join(desc_tokens).strip()

            total_units = float(case_pack) * float(qty or 0.0)
            if (line_net <= 0.0) and (qty is not None) and (unit_price is not None):
                line_net = round(float(qty) * float(unit_price), 2)

            is_void = False
            try:
                qv = float(qty or 0.0)
                ln = float(line_net or 0.0)
            except Exception:
                qv = 0.0
                ln = 0.0
            if qv < 0 or ln < 0:
                is_void = True
            if qv < 0 and ln > 0:
                line_net = -abs(ln)
                ln = float(line_net)
            if ln < 0 and qv > 0:
                qty = -abs(qv)
                total_units = float(case_pack) * float(qty or 0.0)
            if unit_price is not None:
                try:
                    if float(unit_price) < 0:
                        unit_price = abs(float(unit_price))
                except Exception:
                    pass

            vat_amount = round(line_net * (vat_rate / 100.0), 2)
            line_gross = round(line_net + vat_amount, 2)

            pack_size = ""
            try:
                # Prefer PM### token from unit_size or description
                pm = ""
                if unit_size and re.search(r"\bPM\d+\b", str(unit_size), flags=re.IGNORECASE):
                    pm = re.search(r"\bPM\d+\b", str(unit_size), flags=re.IGNORECASE).group(0).upper()
                if (not pm) and description and re.search(r"\bPM\d+\b", description, flags=re.IGNORECASE):
                    pm = re.search(r"\bPM\d+\b", description, flags=re.IGNORECASE).group(0).upper()
                if pm:
                    pack_size = pm
                elif case_pack and unit_size:
                    pack_size = f"{int(case_pack)}x{unit_size}"
                elif case_pack:
                    pack_size = str(int(case_pack))
            except Exception:
                pack_size = ""

            item = {
                "lineNo": None,
                "page": None,
                "code": code_val,
                "description": description,
                "casePack": int(case_pack),
                "unitSize": unit_size,
                "qty": float(qty or 0.0),
                "totalUnits": float(total_units),
                "unitPrice": float(unit_price or 0.0),
                "lineNet": float(line_net),
                "vatCode": vat_code,
                "vatRate": float(vat_rate),
                "vatAmount": float(vat_amount),
                "lineGross": float(line_gross),
                "stdRrp": float(std_rrp) if std_rrp is not None else 0.0,
                "por": float(por) if por is not None else 0.0,
                "isVoid": is_void,
                "packSize": pack_size,
                "PACK": int(case_pack),
                "SIZE": unit_size,
                "CODE": code_val,
                "DESCRIPTION": description,
                "PACK SIZE": pack_size,
                "PACK": int(case_pack),
                "SIZE": unit_size,
                "QTY": float(qty or 0.0),
                "PRICE": float(unit_price or 0.0),
                "VALUE": float(line_net),
                "VAT": vat_code,
                "STD RRP": float(std_rrp) if std_rrp is not None else 0.0,
                "POR": float(por) if por is not None else 0.0,
                "raw": raw_line,
            }
            # If pack_size is PM###, remove it from description for clean display
            try:
                if pack_size and re.fullmatch(r"PM\d+", pack_size, flags=re.IGNORECASE):
                    desc_clean = re.sub(rf"\s*\b{re.escape(pack_size)}\b", "", description, flags=re.IGNORECASE).strip()
                    if desc_clean != description:
                        item["description"] = desc_clean
                        item["DESCRIPTION"] = desc_clean
            except Exception:
                pass
            items.append(item)

        return items


HEADER_WORDS = [
    "description","item","product","qty","quantity","unit","price","net","vat","amount","total","code","pack","size"
]

STOP_WORDS = [
    "subtotal","sub total","sub-total","grand total","amount due","balance due","total due",
    "vat total","total vat","total payable","invoice total",
    "promotions saved you","total savings","savings",
    "registered in england","registered office","equity house","telephone","tel:","www.","company no","company number"
]

HEADER_HINTS = [
    "description", "qty", "quantity", "unit price", "price", "net", "vat", "amount", "total"
]

NUMERIC_RE = re.compile(r"^\(?-?\d{1,3}(?:,\d{3})*(?:\.\d{2})?\)?$")

def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip())

def _is_money(tok: str) -> bool:
    t = tok.strip().replace(",", "")
    return bool(re.fullmatch(r"[£$€]?\-?\d+(\.\d{2})?", t))

def _to_money(tok: str) -> float:
    t = tok.strip().replace(",", "").replace("£","").replace("$","").replace("€","")
    try:
        return float(t)
    except Exception:
        return 0.0

def _is_qty(tok: str) -> bool:
    t = tok.strip()
    return bool(re.fullmatch(r"\d+(\.\d+)?", t))

def _looks_like_code(tok: str) -> bool:
    t = tok.strip()
    if len(t) < 3:
        return False
    return bool(re.fullmatch(r"[A-Z0-9\-]{3,}", t, flags=re.I))

def _contains_any(text: str, words: List[str]) -> bool:
    s = (text or "").lower()
    return any(w in s for w in words)

def _group_lines_by_row(ocr_lines, y_tol: float = 12.0):
    rows = []
    for ln in ocr_lines:
        text = getattr(ln, "text", None) if not isinstance(ln, dict) else ln.get("text")
        bbox = getattr(ln, "bbox", None) if not isinstance(ln, dict) else ln.get("bbox")
        page = getattr(ln, "page", None) if not isinstance(ln, dict) else ln.get("page", 1)
        if not text or not bbox:
            continue
        x0,y0,x1,y1 = map(float, bbox)
        rows.append((int(page), y0, x0, x1, y1, _norm(text)))

    rows.sort(key=lambda r: (r[0], r[1], r[2]))

    groups = []
    cur = []
    cur_key = None
    for page, y0, x0, x1, y1, text in rows:
        if not cur:
            cur = [(x0,y0,x1,y1,text)]
            cur_key = (page, y0)
            continue
        if page == cur_key[0] and abs(y0 - cur_key[1]) <= y_tol:
            cur.append((x0,y0,x1,y1,text))
        else:
            groups.append((cur_key[0], cur))
            cur = [(x0,y0,x1,y1,text)]
            cur_key = (page, y0)
    if cur:
        groups.append((cur_key[0], cur))
    return groups

def _row_text(g):
    return " ".join([x[4] for x in sorted(g, key=lambda z: z[0])]).strip()

def _header_hits(text: str) -> int:
    low = text.lower()
    return sum(1 for w in HEADER_WORDS if w in low)

def _tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"\s+", text) if t.strip()]

def _center_x(b):
    x0, y0, x1, y1 = b
    return (x0 + x1) / 2.0

def _bbox_center(b: List[float]) -> Tuple[float, float]:
    return (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0

def _line_center_in_bbox(bbox: List[float], target_bbox: List[float]) -> bool:
    cx, cy = _bbox_center(bbox)
    x0, y0, x1, y1 = target_bbox
    return x0 <= cx <= x1 and y0 <= cy <= y1

def _parse_pct(text: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%?", text)
    if not m:
        return 0.0
    try:
        return float(m.group(1))
    except Exception:
        return 0.0

def _has_percent(text: str) -> bool:
    return "%" in (text or "")

def _parse_money(text: str) -> float:
    # Use last money-like token in the text
    toks = re.findall(r"[Â£$â‚¬]?\-?\d+(?:\.\d{2})?", text.replace(",", ""))
    if not toks:
        return 0.0
    return _to_money(toks[-1])

def _is_numeric_token(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return bool(NUMERIC_RE.fullmatch(t))

def _to_number(text: str) -> float:
    t = (text or "").strip()
    if not t:
        return 0.0
    t = t.replace(",", "")
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg = True
        t = t[1:-1]
    try:
        val = float(t)
        return -val if neg else val
    except Exception:
        return 0.0

def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2 == 1:
        return float(values[mid])
    return float((values[mid - 1] + values[mid]) / 2.0)

def _line_text(line) -> str:
    return _norm(getattr(line, "text", "") if not isinstance(line, dict) else line.get("text", ""))

def _line_bbox(line) -> Optional[List[float]]:
    bbox = getattr(line, "bbox", None) if not isinstance(line, dict) else line.get("bbox")
    if not bbox:
        return None
    return list(map(float, bbox))

def _line_page(line) -> int:
    return int(getattr(line, "page", 1) if not isinstance(line, dict) else line.get("page", 1))

def _header_score(text: str) -> int:
    low = text.lower()
    return sum(1 for w in HEADER_HINTS if w in low)

def _find_table_bounds(lines) -> Optional[Dict[str, Any]]:
    """
    Find a header row and an optional bottom row (totals) to define the table region.
    Returns {page, top_y, bottom_y} or None.
    """
    if not lines:
        return None

    # sort by page then y
    lines_sorted = sorted(
        lines,
        key=lambda ln: (_line_page(ln), (_line_bbox(ln) or [0, 0, 0, 0])[1]),
    )

    header_idx = None
    for i, ln in enumerate(lines_sorted):
        if _header_score(_line_text(ln)) >= 2:
            header_idx = i
            break
    if header_idx is None:
        return None

    header_ln = lines_sorted[header_idx]
    header_bbox = _line_bbox(header_ln)
    if not header_bbox:
        return None

    header_page = _line_page(header_ln)
    top_y = float(header_bbox[1])

    bottom_y = None
    bottom_page = None
    for ln in lines_sorted[header_idx + 1 :]:
        if _contains_any(_line_text(ln).lower(), STOP_WORDS):
            b = _line_bbox(ln)
            if b:
                bottom_page = _line_page(ln)
                bottom_y = float(b[1])
                break

    return {"page": header_page, "top_y": top_y, "bottom_page": bottom_page, "bottom_y": bottom_y}

def _page_dims_from_words(words: List["OcrWord"], page: int) -> Tuple[float, float]:
    max_x = 0.0
    max_y = 0.0
    for w in words:
        if int(w.page) != int(page):
            continue
        max_x = max(max_x, float(w.bbox[2]))
        max_y = max(max_y, float(w.bbox[3]))
    return max_x, max_y

def _rel_to_px_from_words(bbox_rel: List[float], words: List["OcrWord"], page: int) -> Optional[List[float]]:
    if not bbox_rel or len(bbox_rel) != 4:
        return None
    page_w, page_h = _page_dims_from_words(words, page)
    if page_w <= 0.0 or page_h <= 0.0:
        return None
    x0, y0, x1, y1 = bbox_rel
    return [x0 * page_w, y0 * page_h, x1 * page_w, y1 * page_h]

def _template_columns_px(template: Dict[str, Any], ocr_res) -> Optional[Dict[str, Any]]:
    if not template:
        return None
    items_cfg = template.get("items") or {}
    cols = items_cfg.get("columns") or {}
    if not cols:
        return None

    page = int(items_cfg.get("page") or 1)
    col_ranges: Dict[str, Tuple[float, float]] = {}
    top_y = None

    for name, spec in cols.items():
        bbox_rel = spec.get("bbox_rel")
        bbox_px = _rel_to_px_from_words(bbox_rel, ocr_res.words, page)
        if not bbox_px:
            continue
        x0, y0, x1, y1 = bbox_px
        col_ranges[name] = (float(x0), float(x1))
        if top_y is None or y0 < top_y:
            top_y = float(y0)

    if not col_ranges:
        return None

    return {"page": page, "col_ranges": col_ranges, "top_y": top_y}

def _template_regions_px(template: Dict[str, Any], ocr_res) -> List[Dict[str, Any]]:
    items_cfg = template.get("items") or {}
    regions = items_cfg.get("regions") or []
    out = []
    for reg in regions:
        try:
            page = int(reg.get("page", 1))
            bbox_rel = reg.get("bbox_rel")
            bbox_px = _rel_to_px_from_words(bbox_rel, ocr_res.words, page)
            if not bbox_px:
                continue
            out.append({"page": page, "bbox_px": bbox_px})
        except Exception:
            continue
    return out

def _cluster_words_into_rows(words: List["OcrWord"]) -> List[List["OcrWord"]]:
    if not words:
        return []
    heights = [max(0.0, w.bbox[3] - w.bbox[1]) for w in words]
    median_h = _median([h for h in heights if h > 0.0])
    threshold = 0.6 * median_h if median_h > 0.0 else 6.0

    def cy(w: "OcrWord") -> float:
        return (w.bbox[1] + w.bbox[3]) / 2.0

    words_sorted = sorted(words, key=lambda w: (cy(w), w.bbox[0]))
    rows: List[Tuple[float, List["OcrWord"]]] = []
    for w in words_sorted:
        w_cy = cy(w)
        if not rows:
            rows.append((w_cy, [w]))
            continue
        row_cy, row_words = rows[-1]
        if abs(w_cy - row_cy) > threshold:
            rows.append((w_cy, [w]))
        else:
            row_words.append(w)
            new_cy = (row_cy * (len(row_words) - 1) + w_cy) / float(len(row_words))
            rows[-1] = (new_cy, row_words)
    return [sorted(rw, key=lambda w: w.bbox[0]) for _, rw in rows]

def _cluster_numeric_columns(words: List["OcrWord"], min_count: int = 3) -> List[float]:
    """
    Cluster numeric-ish word x1 positions to infer right-aligned columns.
    Returns sorted list of x1 centers.
    """
    num_words = [w for w in words if _is_numeric_token(w.text)]
    if not num_words:
        return []
    heights = [max(0.0, w.bbox[3] - w.bbox[1]) for w in num_words]
    median_h = _median([h for h in heights if h > 0.0])
    gap = max(10.0, 0.8 * median_h)

    xs = sorted([w.bbox[2] for w in num_words])
    clusters: List[List[float]] = []
    for x in xs:
        if not clusters:
            clusters.append([x])
            continue
        if abs(x - clusters[-1][-1]) <= gap:
            clusters[-1].append(x)
        else:
            clusters.append([x])

    # keep clusters with enough members
    centers = [sum(c) / len(c) for c in clusters if len(c) >= max(1, min_count)]
    return sorted(centers)

def _assign_numeric_to_columns(row_words: List["OcrWord"], col_xs: List[float]) -> List[Optional[float]]:
    vals: List[Optional[float]] = [None for _ in col_xs]
    if not col_xs:
        return vals
    for w in row_words:
        if not _is_numeric_token(w.text):
            continue
        x1 = w.bbox[2]
        # nearest column by x1
        idx = min(range(len(col_xs)), key=lambda i: abs(col_xs[i] - x1))
        v = _to_number(w.text)
        if vals[idx] is None:
            vals[idx] = v
        else:
            # keep the right-most (or larger) value if multiple
            vals[idx] = v
    return vals

def _extract_items_by_layout(ocr_res, template: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    if not hasattr(ocr_res, "words") or not ocr_res.words:
        return []

    lines = getattr(ocr_res, "lines", []) or []
    tpl_cols = _template_columns_px(template or {}, ocr_res)
    tpl_regions = _template_regions_px(template or {}, ocr_res)
    bounds = _find_table_bounds(lines)
    if not bounds and not tpl_cols:
        return []

    if tpl_cols:
        page = tpl_cols["page"]
        top_y = tpl_cols.get("top_y") or (bounds["top_y"] if bounds else 0.0)
        bottom_y = bounds.get("bottom_y") if bounds else None
        bottom_page = bounds.get("bottom_page") if bounds else None
    else:
        page = bounds["page"]
        top_y = float(bounds["top_y"])
        bottom_y = bounds.get("bottom_y")
        bottom_page = bounds.get("bottom_page")

    # Filter words to the table region
    words = []
    if tpl_regions:
        for w in ocr_res.words:
            for reg in tpl_regions:
                if int(w.page) != int(reg["page"]):
                    continue
                if _line_center_in_bbox(list(map(float, w.bbox)), list(map(float, reg["bbox_px"]))):
                    words.append(w)
                    break
    else:
        for w in ocr_res.words:
            w_page = int(w.page)
            if w_page < int(page):
                continue
            if bottom_page is not None and w_page > int(bottom_page):
                continue
            cy = (w.bbox[1] + w.bbox[3]) / 2.0
            if w_page == int(page) and cy <= top_y:
                continue
            if bottom_page is not None and w_page == int(bottom_page) and bottom_y is not None and cy >= float(bottom_y):
                continue
            words.append(w)

    if not words:
        return []

    rows = _cluster_words_into_rows(words)
    col_xs = [] if tpl_cols else _cluster_numeric_columns(words, min_count=3)

    items: List[Dict[str, Any]] = []
    line_no = 0
    for row_words in rows:
        if tpl_cols:
            col_ranges = tpl_cols["col_ranges"]
            col_text: Dict[str, List[str]] = {}
            for w in row_words:
                xc = (w.bbox[0] + w.bbox[2]) / 2.0
                for col_name, (x0, x1) in col_ranges.items():
                    if x0 <= xc <= x1:
                        col_text.setdefault(col_name, []).append(w.text)
                        break

            code = " ".join(col_text.get("code", [])).strip()
            description = " ".join(col_text.get("description", [])).strip()
            qty = _to_number(" ".join(col_text.get("qty", [])).strip()) if col_text.get("qty") else 0.0
            unit_price = _to_number(" ".join(col_text.get("unit_price", [])).strip()) if col_text.get("unit_price") else 0.0
            if not unit_price and col_text.get("price"):
                unit_price = _to_number(" ".join(col_text.get("price", [])).strip())
            line_net = _to_number(" ".join(col_text.get("net", [])).strip()) if col_text.get("net") else 0.0
            vat_amount = _to_number(" ".join(col_text.get("vat", [])).strip()) if col_text.get("vat") else 0.0
            line_gross = _to_number(" ".join(col_text.get("total", [])).strip()) if col_text.get("total") else 0.0
            if not line_gross and col_text.get("amount"):
                line_gross = _to_number(" ".join(col_text.get("amount", [])).strip())

            if not description and code:
                # if description wasn't annotated but code was, skip row
                continue
            if not description:
                continue
            if not any(v > 0 for v in [qty, unit_price, line_net, vat_amount, line_gross]):
                continue
        else:
            # Build description from left of first numeric column (if any)
            if col_xs:
                left_words = [w for w in row_words if w.bbox[2] < (col_xs[0] - 4.0)]
            else:
                left_words = row_words
            description = " ".join([w.text for w in left_words]).strip()

            numeric_vals = _assign_numeric_to_columns(row_words, col_xs)
            numeric_vals_clean = [v for v in numeric_vals if v is not None]

            if not description or not numeric_vals_clean:
                continue

            # Try to split leading item code from description only if numeric
            m = re.match(r"^([0-9]{4,})\s+(.*)$", description)
            code = ""
            if m:
                code = m.group(1).strip()
                description = m.group(2).strip()

            qty = 0.0
            unit_price = 0.0
            line_net = 0.0
            vat_amount = 0.0
            line_gross = 0.0

            if len(numeric_vals) == 1:
                line_gross = numeric_vals[0] or 0.0
            elif len(numeric_vals) == 2:
                unit_price = numeric_vals[0] or 0.0
                line_gross = numeric_vals[1] or 0.0
            elif len(numeric_vals) == 3:
                qty = numeric_vals[0] or 0.0
                unit_price = numeric_vals[1] or 0.0
                line_gross = numeric_vals[2] or 0.0
            elif len(numeric_vals) == 4:
                qty = numeric_vals[0] or 0.0
                unit_price = numeric_vals[1] or 0.0
                line_net = numeric_vals[2] or 0.0
                line_gross = numeric_vals[3] or 0.0
            else:
                qty = numeric_vals[0] or 0.0
                unit_price = numeric_vals[1] or 0.0
                line_net = numeric_vals[2] or 0.0
                vat_amount = numeric_vals[3] or 0.0
                line_gross = numeric_vals[4] or 0.0

        if not line_net and qty and unit_price:
            line_net = round(qty * unit_price, 2)

        line_no += 1
        items.append(
            {
                "lineNo": line_no,
                "page": int(page),
                "code": code or "-",
                "description": description,
                "qty": qty,
                "unitPrice": unit_price,
                "lineNet": line_net,
                "vatRate": 0.0,
                "vatAmount": vat_amount,
                "lineGross": line_gross,
                "raw": " ".join([w.text for w in row_words]).strip(),
            }
        )

    return items

def _find_header_columns(groups):
    # Look for a header row that contains the common Booker columns
    def _norm_hdr(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (t or "").lower())

    want = {
        "code": ["code", "itemcode", "item#", "itmcode"],
        "description": ["description", "desc", "item", "product"],
        "pack_size": ["packsize", "pack", "size", "casesize", "case", "pk"],
        "qty": ["qty", "quantity", "qnty", "q'ty"],
        "price": ["price", "unitprice", "unit"],
        "value": ["value", "amount", "total", "net"],
        "vat": ["vat", "vatcode", "vat%"],
        "std_rrp": ["stdrrp", "rrp", "std"],
        "por": ["por", "p.o.r", "profit"],
    }

    want_norm = {k: {_norm_hdr(w) for w in v} for k, v in want.items()}

    for page, g in groups:
        # tokens ordered left-to-right for multi-word header detection
        toks = [(x0, y0, x1, y1, text) for (x0, y0, x1, y1, text) in sorted(g, key=lambda z: z[0])]
        txt = _row_text(g)
        low = txt.lower()

        hits = 0
        for key, words in want.items():
            if any(w in low for w in words):
                hits += 1
        if hits < 3:
            continue

        # map column centers based on header tokens
        centers = {}
        for i, (x0, y0, x1, y1, text) in enumerate(toks):
            t_norm = _norm_hdr(text)
            for key, words_norm in want_norm.items():
                if key in centers:
                    continue
                if t_norm in words_norm and t_norm:
                    centers[key] = _center_x((x0, y0, x1, y1))

            # handle split headers like "PACK SIZE", "STD RRP", "UNIT PRICE"
            if i + 1 < len(toks):
                nx0, ny0, nx1, ny1, ntext = toks[i + 1]
                n_norm = _norm_hdr(ntext)
                pair = t_norm + n_norm
                if "pack_size" not in centers and pair == "packsize":
                    centers["pack_size"] = _center_x((x0, y0, nx1, ny1))
                if "std_rrp" not in centers and pair in ("stdrrp", "rrpstd"):
                    centers["std_rrp"] = _center_x((x0, y0, nx1, ny1))
                if "price" not in centers and pair == "unitprice":
                    centers["price"] = _center_x((x0, y0, nx1, ny1))

        if len(centers) >= 3:
            return page, centers
    return None, {}

def _assign_to_column(xc: float, centers: Dict[str, float]) -> str:
    best = None
    best_d = 1e9
    for k, cx in centers.items():
        d = abs(xc - cx)
        if d < best_d:
            best_d = d
            best = k
    return best or ""

def _extract_items_booker(ocr_lines) -> List[Dict[str, Any]]:
    groups = _group_lines_by_row(ocr_lines, y_tol=12.0)
    header_page, centers = _find_header_columns(groups)
    if not centers:
        return []

    items: List[Dict[str, Any]] = []
    line_no = 0
    prev_incomplete = None

    for page, g in groups:
        if page < header_page:
            continue
        txt = _row_text(g)
        low = txt.lower()
        if _contains_any(low, STOP_WORDS):
            break

        # Build column text by nearest header center
        col_text: Dict[str, List[str]] = {}
        for x0,y0,x1,y1,text in g:
            xc = _center_x((x0,y0,x1,y1))
            col = _assign_to_column(xc, centers)
            col_text.setdefault(col, []).append(text)

        code = " ".join(col_text.get("code", [])).strip()
        desc = " ".join(col_text.get("description", [])).strip()
        pack_size = " ".join(col_text.get("pack_size", [])).strip()
        qty_txt = " ".join(col_text.get("qty", [])).strip()
        price_txt = " ".join(col_text.get("price", [])).strip()
        value_txt = " ".join(col_text.get("value", [])).strip()
        vat_txt = " ".join(col_text.get("vat", [])).strip()
        std_rrp_txt = " ".join(col_text.get("std_rrp", [])).strip()
        por_txt = " ".join(col_text.get("por", [])).strip()

        qty = _parse_money(qty_txt) if qty_txt else 0.0
        unit_price = _parse_money(price_txt) if price_txt else 0.0
        line_net = _parse_money(value_txt) if value_txt else 0.0
        std_rrp = _parse_money(std_rrp_txt) if std_rrp_txt else 0.0
        por = _parse_pct(por_txt) if por_txt else 0.0

        vat_rate = 0.0
        vat_amount = 0.0
        vat_code = ""
        if vat_txt:
            if re.search(r"[A-Za-z]", vat_txt) and not re.search(r"\d", vat_txt):
                vat_code = vat_txt.strip()
            elif _has_percent(vat_txt):
                vat_rate = _parse_pct(vat_txt)
            else:
                # Booker VAT column can be amount without a % sign
                vat_amount = _parse_money(vat_txt)
        line_gross = 0.0

        # Fallback: parse VAT/STD RRP/POR (and pack/unit) from raw row text (common Booker layout)
        if (not vat_code) or (std_rrp <= 0.0) or (por <= 0.0) or (not pack_size):
            toks = [t for t in re.split(r"\s+", txt) if t.strip()]

            def _is_flag(t: str) -> bool:
                return t.strip().upper() in ("P", "SL", "S/L", "SK", "SH", "SI", "S1")

            def _is_vat_code(t: str) -> bool:
                return bool(re.fullmatch(r"(B|A|Z|Z/A|R)", t.strip().upper()))

            def _is_unitsize(t: str) -> bool:
                return bool(re.fullmatch(r"\d+(?:\.\d+)?\s*(ML|L|G|KG|CL)", t.strip().upper()))

            def _as_num_strict(t: str) -> float:
                s = (t or "").strip()
                if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
                    return 0.0
                try:
                    return float(s)
                except Exception:
                    return 0.0

            # POR: last % token or last numeric token (OCR may omit %)
            if por <= 0.0:
                for t in reversed(toks):
                    if "%" in t:
                        por = _parse_pct(t)
                        if por:
                            break
            if por <= 0.0:
                for t in reversed(toks):
                    if re.fullmatch(r"\d+(?:\.\d+)?", t):
                        try:
                            por = float(t)
                            break
                        except Exception:
                            break

            # VAT code: look for single-letter codes near end
            if not vat_code:
                for t in reversed(toks):
                    if _is_vat_code(t):
                        vat_code = t.strip().upper()
                        break

            # STD RRP: numeric token immediately after VAT code if present
            if std_rrp <= 0.0 and vat_code:
                try:
                    idxs = [i for i, t in enumerate(toks) if t.strip().upper() == vat_code]
                    if idxs:
                        i = idxs[-1]
                        for j in range(i + 1, min(i + 4, len(toks))):
                            v = _parse_money(toks[j])
                            if v > 0:
                                std_rrp = v
                                break
                except Exception:
                    pass

            # Try to parse pack_size/unit_size/qty/price/value from raw token order
            try:
                # Find VAT code index scanning from right
                vat_idx = None
                for i in range(len(toks) - 1, 0, -1):
                    if _is_vat_code(toks[i]):
                        vat_idx = i
                        break
                if vat_idx is not None:
                    # VALUE: last strict numeric token before VAT
                    value_idx = None
                    for j in range(vat_idx - 1, 1, -1):
                        if _as_num_strict(toks[j]) > 0:
                            value_idx = j
                            break
                    if value_idx is not None and line_net <= 0.0:
                        line_net = _as_num_strict(toks[value_idx])

                    # Optional flag between price and value
                    scan_left = value_idx - 1 if value_idx is not None else None
                    if scan_left is not None and scan_left >= 0 and _is_flag(toks[scan_left]):
                        scan_left -= 1

                    # PRICE: nearest strict numeric to left of VALUE
                    if scan_left is not None and scan_left >= 0 and unit_price <= 0.0:
                        for j in range(scan_left, 1, -1):
                            v = _as_num_strict(toks[j])
                            if v > 0:
                                unit_price = v
                                price_idx = j
                                break
                        else:
                            price_idx = None
                    else:
                        price_idx = None

                    # QTY: nearest strict numeric to left of price
                    if price_idx is not None and qty <= 0.0:
                        for j in range(price_idx - 1, 1, -1):
                            v = _as_num_strict(toks[j])
                            if v > 0:
                                qty = v
                                qty_idx = j
                                break
                        else:
                            qty_idx = None
                    else:
                        qty_idx = None

                    # UNIT SIZE (immediately before qty) and CASE PACK (before unit size)
                    if qty_idx is not None:
                        # expect unit size immediately before qty
                        if qty_idx - 1 >= 0 and _is_unitsize(toks[qty_idx - 1]):
                            unit_size = toks[qty_idx - 1].replace(" ", "")
                        else:
                            unit_size = ""
                    else:
                        unit_size = ""

                    # PACK SIZE from description token (PM###) if still empty
                    if not pack_size:
                        for t in toks:
                            if re.fullmatch(r"PM\d+", t.upper()):
                                pack_size = t.upper()
                                break
            except Exception:
                pass

        # Fallback: derive pack size from description if missing
        if not pack_size and desc:
            d = desc.upper()
            m = re.search(r"\bPM\d+\b", d)
            if m:
                pack_size = m.group(0)
            else:
                m = re.search(r"\b\d+(?:\.\d+)?\s*(ML|L|G|KG|CL)\b", d)
                if m:
                    pack_size = m.group(0).replace(" ", "")

        # If pack_size was found, remove it from description (common Booker OCR quirk)
        if pack_size and desc:
            desc = re.sub(rf"\s*\b{re.escape(pack_size)}\b\s*$", "", desc, flags=re.IGNORECASE).strip()

        # Booker sanity: if qty>1 and value equals price, value likely missing qty multiplier
        if qty and unit_price and (line_net <= 0.0 or abs(line_net - unit_price) < 0.01) and qty > 1:
            line_net = round(float(qty) * float(unit_price), 2)

        # If this row looks like a continuation of description, merge
        has_amounts = (qty > 0 or unit_price > 0 or line_net > 0 or vat_amount > 0 or line_gross > 0)
        if desc and not has_amounts and prev_incomplete is not None:
            prev_incomplete["description"] = (prev_incomplete.get("description","") + " " + desc).strip()
            continue

        if not desc or not has_amounts:
            continue

        line_no += 1
        if line_gross > 0:
            if vat_rate > 0:
                line_net = round(line_gross / (1.0 + (vat_rate / 100.0)), 2)
                vat_amount = round(line_gross - line_net, 2)
            elif vat_amount > 0:
                line_net = round(line_gross - vat_amount, 2)
                if line_net > 0:
                    vat_rate = round((vat_amount / line_net) * 100.0, 2)
        if line_net <= 0 and unit_price and qty:
            line_net = round(unit_price * qty, 2)
            if vat_rate > 0:
                vat_amount = round(line_net * (vat_rate / 100.0), 2)
                if line_gross <= 0:
                    line_gross = round(line_net + vat_amount, 2)
            elif vat_amount > 0:
                if line_gross <= 0:
                    line_gross = round(line_net + vat_amount, 2)
                if line_net > 0 and vat_rate <= 0:
                    vat_rate = round((vat_amount / line_net) * 100.0, 2)
        if line_net <= 0 and line_gross > 0 and vat_rate <= 0 and vat_amount <= 0:
            line_net = line_gross

        item = {
            "lineNo": line_no,
            "page": page,
            "code": code,
            "packSize": pack_size,
            "description": desc,
            "qty": qty,
            "unitPrice": unit_price,
            "lineNet": line_net or 0.0,
            "vatRate": vat_rate or 0.0,
            "vatAmount": vat_amount or 0.0,
            "vatCode": vat_code,
            "lineGross": line_gross or 0.0,
            "stdRrp": std_rrp,
            "por": por,
            "CODE": code,
            "DESCRIPTION": desc,
            "PACK SIZE": pack_size,
            "QTY": qty,
            "PRICE": unit_price,
            "VALUE": line_net or 0.0,
            "VAT": vat_code or (vat_rate or vat_amount),
            "STD RRP": std_rrp,
            "POR": por,
            "raw": txt,
        }
        items.append(item)
        prev_incomplete = item

    return items


def _enrich_booker_items_from_raw(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Booker raw lines often contain: VAT_CODE STD_RRP POR%
    Example: "... 13.49 13.49 A 1.35 33.4%"
    """
    if not items:
        return items
    for it in items:
        if not isinstance(it, dict):
            continue
        raw = str(it.get("raw") or "").strip()
        if not raw:
            continue
        toks = [t for t in re.split(r"\s+", raw) if t.strip()]

        def _as_num_strict(t: str) -> float:
            s = (t or "").strip()
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", s):
                return 0.0
            try:
                return float(s)
            except Exception:
                return 0.0

        def _is_flag(t: str) -> bool:
            return t.strip().upper() in ("P", "SL", "S/L", "SK", "SH", "SI", "S1")

        def _is_vat_code(t: str) -> bool:
            return bool(re.fullmatch(r"(B|A|Z|Z/A|R)", t.strip().upper()))

        # Fill pack size from PM### if missing
        if not it.get("packSize"):
            m = re.search(r"\bPM\d+\b", raw.upper())
            if m:
                it["packSize"] = m.group(0)

        # Strip pack size token from description if it got appended
        try:
            ps = str(it.get("packSize") or "").strip()
            if ps:
                desc0 = str(it.get("description") or "")
                desc1 = re.sub(rf"\s*\b{re.escape(ps)}\b\s*$", "", desc0, flags=re.IGNORECASE).strip()
                if desc1 != desc0:
                    it["description"] = desc1
                    # Keep Booker display column in sync when present
                    if "DESCRIPTION" in it and isinstance(it.get("DESCRIPTION"), str):
                        it["DESCRIPTION"] = desc1
        except Exception:
            pass
        # Fill VAT / STD RRP / POR if missing
        if (not it.get("vatCode")) or not (it.get("stdRrp")) or not (it.get("por")):
            m = re.search(r"\b([A-Z](?:/A)?)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)(?:\s*%|\b)", raw.upper())
            if m:
                vc = m.group(1)
                rrp = m.group(2)
                por = m.group(3)
                if not it.get("vatCode"):
                    it["vatCode"] = vc
                try:
                    if not it.get("stdRrp"):
                        it["stdRrp"] = float(rrp)
                except Exception:
                    pass
                try:
                    if not it.get("por"):
                        it["por"] = float(por)
                except Exception:
                    pass
            else:
                # Fallback: scan tokens for VAT code then next two numbers
                toks2 = [t for t in re.split(r"\s+", raw.upper()) if t.strip()]
                def _is_vat2(t: str) -> bool:
                    return bool(re.fullmatch(r"(B|A|Z|Z/A|R)", t))
                def _num2(t: str) -> float | None:
                    t2 = t.replace("%", "")
                    if re.fullmatch(r"\d+(?:\.\d+)?", t2):
                        try:
                            return float(t2)
                        except Exception:
                            return None
                    return None
                for i in range(len(toks2) - 1, -1, -1):
                    if _is_vat2(toks2[i]):
                        nums = []
                        for j in range(i + 1, min(i + 5, len(toks2))):
                            n = _num2(toks2[j])
                            if n is not None:
                                nums.append(n)
                        if len(nums) >= 2:
                            if not it.get("vatCode"):
                                it["vatCode"] = toks2[i]
                            if not it.get("stdRrp"):
                                it["stdRrp"] = float(nums[0])
                            if not it.get("por"):
                                it["por"] = float(nums[1])
                            break

        # Try to correct qty/price/value from raw token order
        try:
            vat_idx = None
            for i in range(len(toks) - 1, 0, -1):
                if _is_vat_code(toks[i]):
                    vat_idx = i
                    break
            if vat_idx is not None:
                value_idx = None
                for j in range(vat_idx - 1, 1, -1):
                    if _as_num_strict(toks[j]) > 0:
                        value_idx = j
                        break
                if value_idx is not None:
                    line_net = _as_num_strict(toks[value_idx])
                    if line_net > 0:
                        it["lineNet"] = line_net

                    scan_left = value_idx - 1
                    if scan_left >= 0 and _is_flag(toks[scan_left]):
                        scan_left -= 1

                    price_idx = None
                    for j in range(scan_left, 1, -1):
                        v = _as_num_strict(toks[j])
                        if v > 0:
                            price_idx = j
                            it["unitPrice"] = v
                            break

                    if price_idx is not None:
                        for j in range(price_idx - 1, 1, -1):
                            v = _as_num_strict(toks[j])
                            if v > 0:
                                it["qty"] = v
                                break
        except Exception:
            pass
    return items

def _row_score(text: str) -> int:
    """
    Score a row as a likely item line.
    +2 if has >=2 money tokens
    +1 if has qty token
    +1 if has a plausible description length
    -3 if contains stop/footer words
    """
    low = text.lower()
    if _contains_any(low, STOP_WORDS):
        return -3
    toks = _tokenize(text)
    money = sum(1 for t in toks if _is_money(t))
    qty = sum(1 for t in toks if _is_qty(t))
    desc_len = len(re.sub(r"[^A-Za-z ]", "", text).strip())

    score = 0
    if money >= 2:
        score += 2
    if qty >= 1:
        score += 1
    if desc_len >= 8:
        score += 1
    return score

def extract_items_any_supplier(ocr_input, template: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    # If full OCR result is provided, try layout-based table extraction first.
    if OcrResult is not None and isinstance(ocr_input, OcrResult):
        # Booker text-layer/OCR full text parse (more reliable for pack/qty/VAT)
        if getattr(ocr_input, "full_text", "") and _looks_like_booker_invoice(ocr_input.full_text):
            booker_items_text = BookerCombinedRowParser().parse(ocr_input.full_text)
            if booker_items_text:
                return _enrich_booker_items_from_raw(booker_items_text)
        # Booker has a very specific layout; try it before generic layout parsing.
        booker_items = _extract_items_booker(ocr_input.lines)
        if booker_items:
            return booker_items
        items = _extract_items_by_layout(ocr_input, template=template)
        if items:
            return items
        ocr_lines = ocr_input.lines
    else:
        ocr_lines = ocr_input

    # Try Booker-style table first (Description/Qty/Price/VAT/Total columns)
    booker_items = _extract_items_booker(ocr_lines)
    if booker_items:
        return _enrich_booker_items_from_raw(booker_items)

    groups = _group_lines_by_row(ocr_lines, y_tol=12.0)

    # 1) Find best header row and its page
    best_i = None
    best_hits = 0
    header_page = None
    for i, (page, g) in enumerate(groups):
        txt = _row_text(g)
        hits = _header_hits(txt)
        if hits > best_hits:
            best_hits = hits
            best_i = i
            header_page = page

    # Require some confidence of header; otherwise fallback to page 1 after some lines
    if best_hits < 2 or best_i is None:
        start_i = 15  # fallback
        header_page = groups[0][0] if groups else 1
    else:
        start_i = best_i + 1

    items: List[Dict[str, Any]] = []
    line_no = 0

    # 2) Only parse pages starting from header_page (avoid footer-only page 2 unless table continues)
    # We'll allow header_page and subsequent pages, but stop when we hit totals/footer strongly.
    for page, g in groups[start_i:]:
        txt = _row_text(g)
        low = txt.lower()

        # stop if totals/footer appears
        if _contains_any(low, STOP_WORDS):
            break

        # if page jumps far beyond header and looks like non-item page, stop
        if page > (header_page + 2) and _row_score(txt) <= 0:
            break

        # ignore very short lines
        if len(txt) < 6:
            continue

        # validate row
        if _row_score(txt) < 2:
            continue

        toks = _tokenize(txt)
        money_tokens = [(i,t) for i,t in enumerate(toks) if _is_money(t)]
        qty_tokens = [(i,t) for i,t in enumerate(toks) if _is_qty(t)]

        line_total = _to_money(money_tokens[-1][1]) if money_tokens else 0.0
        unit_price = _to_money(money_tokens[-2][1]) if len(money_tokens) >= 2 else 0.0

        qty = 0.0
        if qty_tokens:
            last_money_pos = money_tokens[-1][0] if money_tokens else 10**9
            candidates = [qt for qt in qty_tokens if qt[0] < last_money_pos]
            pick = candidates[0] if candidates else qty_tokens[0]
            try:
                qty = float(pick[1])
            except Exception:
                qty = 0.0

        code = toks[0] if _looks_like_code(toks[0]) else ""

        # description: from after code until first numeric token
        first_num_pos = min(
            [p for p,_ in (money_tokens + qty_tokens)] + [len(toks)]
        )
        desc_start = 1 if code else 0
        desc_end = max(desc_start, first_num_pos)
        description = " ".join(toks[desc_start:desc_end]).strip()

        if not description:
            continue

        line_no += 1
        items.append({
            "lineNo": line_no,
            "page": page,
            "code": code,
            "description": description,
            "qty": qty,
            "unitPrice": unit_price,
            "lineNet": line_total,
            "raw": txt,
        })

    return _enrich_booker_items_from_raw(items)

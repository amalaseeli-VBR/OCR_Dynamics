from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


COLUMN_ORDER = ["code", "description", "pack_size", "qty", "price", "value", "vat", "rrp_por"]


def _load_payload(json_path: Path) -> Dict[str, Any]:
    return json.loads(json_path.read_text(encoding="utf-8"))


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _bbox_dict(line_or_bbox: Any) -> Dict[str, float]:
    bbox = line_or_bbox.get("bbox") if isinstance(line_or_bbox, dict) and "bbox" in line_or_bbox else line_or_bbox
    if isinstance(bbox, dict):
        return {
            "left": _safe_float(bbox.get("left")),
            "top": _safe_float(bbox.get("top")),
            "right": _safe_float(bbox.get("right")),
            "bottom": _safe_float(bbox.get("bottom")),
        }
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        return {
            "left": _safe_float(bbox[0]),
            "top": _safe_float(bbox[1]),
            "right": _safe_float(bbox[2]),
            "bottom": _safe_float(bbox[3]),
        }
    return {"left": 0.0, "top": 0.0, "right": 0.0, "bottom": 0.0}


def _mid_y(line: Dict[str, Any]) -> float:
    bbox = _bbox_dict(line)
    return (_safe_float(bbox.get("top")) + _safe_float(bbox.get("bottom"))) / 2.0


def _mid_x(line: Dict[str, Any]) -> float:
    bbox = _bbox_dict(line)
    return (_safe_float(bbox.get("left")) + _safe_float(bbox.get("right"))) / 2.0


def _left(line: Dict[str, Any]) -> float:
    return _safe_float(_bbox_dict(line).get("left"))


def _is_header_text(text: str) -> bool:
    txt = re.sub(r"\s+", " ", str(text or "").upper()).strip()
    return txt in {
        "CODE",
        "DESCRIPTION",
        "PACK SIZE",
        "QTY",
        "PRICE",
        "VALUE",
        "VAT",
        "RRP POR",
        "VAT RRP",
        "VAT RRP POR",
    }


def _find_column_headers(page_lines: List[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
    headers: Dict[str, Dict[str, Any]] = {}
    rrp_header: Optional[Dict[str, Any]] = None
    por_header: Optional[Dict[str, Any]] = None
    for line in page_lines:
        text = re.sub(r"\s+", " ", str(line.get("text") or "").upper()).strip()
        if text == "CODE":
            headers.setdefault("code", line)
        elif text == "DESCRIPTION":
            headers.setdefault("description", line)
        elif text == "PACK SIZE":
            headers.setdefault("pack_size", line)
        elif text == "QTY":
            headers.setdefault("qty", line)
        elif text == "PRICE":
            headers.setdefault("price", line)
        elif text == "VALUE":
            headers.setdefault("value", line)
        elif text == "VAT":
            headers.setdefault("vat", line)
        elif text == "RRP":
            if rrp_header is None:
                rrp_header = line
        elif text == "POR":
            if por_header is None:
                por_header = line
        elif text in {"RRP POR", "VAT RRP", "VAT RRP POR"}:
            if text == "VAT RRP POR":
                # The "VAT RRP POR" bounding box spans both the VAT column (left) and the
                # RRP/POR column (right). Splitting at 1/3 puts the VAT centre near the
                # standalone VAT-letter tokens (X ≈ 0.81) and the RRP/POR centre near the
                # combined "1.65 33.4%" tokens (X ≈ 0.91). Without this split both columns
                # share the same centre X, the sort order is ambiguous, and the ranges end
                # up inverted — causing RRP/POR values to be classified as VAT and vice versa.
                bbox = _bbox_dict(line)
                bl = _safe_float(bbox.get("left"))
                br = _safe_float(bbox.get("right"))
                bt = _safe_float(bbox.get("top"))
                bb = _safe_float(bbox.get("bottom"))
                split_x = bl + (br - bl) / 3.0
                headers.setdefault("vat", {
                    "text": "VAT",
                    "bbox": {"left": bl, "right": split_x, "top": bt, "bottom": bb},
                })
                headers.setdefault("rrp_por", {
                    "text": "RRP POR",
                    "bbox": {"left": split_x, "right": br, "top": bt, "bottom": bb},
                })
            else:
                headers.setdefault("rrp_por", line)
                if text.startswith("VAT"):
                    headers.setdefault("vat", line)
    if "rrp_por" not in headers and (rrp_header or por_header):
        lefts = []
        rights = []
        tops = []
        bottoms = []
        for part in (rrp_header, por_header):
            if not part:
                continue
            bbox = _bbox_dict(part)
            lefts.append(_safe_float(bbox.get("left")))
            rights.append(_safe_float(bbox.get("right")))
            tops.append(_safe_float(bbox.get("top")))
            bottoms.append(_safe_float(bbox.get("bottom")))
        if lefts and rights and tops and bottoms:
            headers["rrp_por"] = {
                "text": "RRP POR",
                "bbox": {
                    "left": min(lefts),
                    "right": max(rights),
                    "top": min(tops),
                    "bottom": max(bottoms),
                },
            }
    if "vat" not in headers and "rrp_por" in headers:
        headers["vat"] = headers["rrp_por"]
    if len(headers) < 7:
        return None
    return headers


def _page_lines(payload: Dict[str, Any], page: int) -> List[Dict[str, Any]]:
    return [line for line in list(payload.get("ocr_lines") or []) if int(line.get("page") or 1) == page]


def _column_ranges(headers: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple[float, float]]:
    centers = [(name, _mid_x(line)) for name, line in headers.items() if name in COLUMN_ORDER]
    centers.sort(key=lambda item: item[1])
    boundaries: List[float] = [0.0]
    for idx in range(len(centers) - 1):
        boundaries.append((centers[idx][1] + centers[idx + 1][1]) / 2.0)
    boundaries.append(1.0)

    ranges: Dict[str, Tuple[float, float]] = {}
    for idx, (name, _) in enumerate(centers):
        ranges[name] = (boundaries[idx], boundaries[idx + 1])
    return ranges


def _assign_column(x_center: float, ranges: Dict[str, Tuple[float, float]]) -> str:
    for name in COLUMN_ORDER:
        if name not in ranges:
            continue
        left, right = ranges[name]
        if left <= x_center < right:
            return name
    return COLUMN_ORDER[-1]


_NOISE_EXACT = {
    "VOID NOTE",
    "SAVING DETAILS",
    "STD",
    "VAT",
    "AIVD",
    "AHSY",
}

_NOISE_STARTSWITH = (
    "EXC.VAT",
    "/CONT",
    "PAGE ",
    "PROMOTIONS SAVED YOU",
    "CLUB PROMOTIONS SAVED YOU",
    "TOTAL SAVINGS",
    "PLEASE DO NOT BRING BACK",
    "TRAYS AND KEGS",
)


def _is_footer_or_noise(line: Dict[str, Any], header_y: float) -> bool:
    text = re.sub(r"\s+", " ", str(line.get("text") or "").upper()).strip()
    left = _left(line)
    y = _mid_y(line)
    if _is_header_text(text):
        return True
    if y <= header_y:
        return True
    if text in _NOISE_EXACT:
        return True
    if any(text.startswith(p) for p in _NOISE_STARTSWITH):
        return True
    if left < 0.03 and text in {"VAT", "STD"}:
        return True
    return False


_RRP_POR_RE = re.compile(r"^\d+\.\d+\s+\d+\.\d+%$")
_PRODUCT_CODE_RE = re.compile(r"^\d{5,}")
_MONEY_RE = re.compile(r"^\d+\.\d+$")
_PERCENT_RE = re.compile(r"^\d+\.\d+%$")
_VAT_CODE_ONLY_RE = re.compile(r"^[ABZR]$", re.IGNORECASE)
_LEADING_MONEY_RE = re.compile(r"^-?\d+\.\d+")
_PM_RE = re.compile(r"(?<!\d)PM(\d{2,4})\b", re.IGNORECASE)


def _looks_like_rrp_por(text: str) -> bool:
    return bool(_RRP_POR_RE.match(text.strip()))


def _looks_like_rrp_part(text: str) -> bool:
    return bool(_MONEY_RE.match(text.strip()))


def _looks_like_por_part(text: str) -> bool:
    return bool(_PERCENT_RE.match(text.strip()))


def _looks_like_product_code_line(text: str) -> bool:
    return bool(_PRODUCT_CODE_RE.match(text.strip()))


def _looks_like_vat_code(text: str) -> bool:
    return bool(_VAT_CODE_ONLY_RE.match(str(text or "").strip()))


def _extract_money_token(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    match = _LEADING_MONEY_RE.match(cleaned)
    return match.group(0) if match else ""


def _parse_qty_number(text: str) -> Optional[float]:
    cleaned = str(text or "").strip()
    match = re.match(r"^-?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def _infer_value_from_price_qty(price: str, qty: str) -> str:
    price_value = _extract_money_token(price)
    qty_value = _parse_qty_number(qty)
    if not price_value or qty_value is None:
        return ""
    try:
        total = float(price_value) * qty_value
    except Exception:
        return ""
    return f"{total:.2f}"


def _infer_price_from_value_qty(value: str, qty: str) -> str:
    value_token = _extract_money_token(value)
    qty_value = _parse_qty_number(qty)
    if not value_token or qty_value in (None, 0):
        return ""
    try:
        price = float(value_token) / qty_value
    except Exception:
        return ""
    return f"{price:.2f}"


def _infer_pmp_from_description(text: str) -> str:
    match = _PM_RE.search(str(text or ""))
    if not match:
        return ""
    try:
        return f"{int(match.group(1)) / 100:.2f}"
    except Exception:
        return ""


def _group_rows(
    lines: List[Dict[str, Any]],
    ranges: Dict[str, Tuple[float, float]],
    tolerance: float = 0.0065,
) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    heights = [
        max(0.0, _safe_float(_bbox_dict(line).get("bottom")) - _safe_float(_bbox_dict(line).get("top")))
        for line in list(lines or [])
        if isinstance(line, dict)
    ]
    heights = [h for h in heights if h > 0]
    # OCR tables often place right-side numeric tokens slightly higher than the
    # left-side code/description text on the same physical row. Use a modest
    # height-aware tolerance so those fragments stay together, while the
    # duplicate-column guards below still split true adjacent rows.
    if heights:
        heights.sort()
        median_height = heights[len(heights) // 2]
        tolerance = max(tolerance, min(0.012, median_height * 0.6))
    for line in sorted(lines, key=lambda item: (_mid_y(item), _left(item))):
        y = _mid_y(line)
        text = str(line.get("text") or "").strip()
        column = _assign_column(_mid_x(line), ranges)
        if not groups:
            groups.append([line])
            continue
        prev_y = sum(_mid_y(item) for item in groups[-1]) / len(groups[-1])
        if abs(y - prev_y) <= tolerance:
            # Guard: if this token is in a right-edge column and the current group already has
            # code/description tokens whose average Y is BELOW this token's Y, this token
            # belongs to the next product (the wine/spirits section OCR places right-edge
            # tokens for product N+1 just below product N's code). Start a new orphan group.
            if column in {"vat", "rrp_por"}:
                identity_ys = [
                    _mid_y(item)
                    for item in groups[-1]
                    if _assign_column(_mid_x(item), ranges) in {"code", "description"}
                ]
                if identity_ys and y > sum(identity_ys) / len(identity_ys):
                    groups.append([line])
                    continue
            # Guard: if this token looks like a combined "RRP POR" value (e.g. "1.65 33.4%")
            # and the current group already has one, the next row's rrp_por is drifting in —
            # start a new group rather than silently absorbing it.
            if _looks_like_rrp_por(text):
                if any(_looks_like_rrp_por(str(l.get("text", "")).strip()) for l in groups[-1]):
                    groups.append([line])
                    continue
            # Guard: if this token looks like a product code line (starts with 5+ digits)
            # and the current group already has one, a new item's code is drifting in —
            # start a new group so each product stays isolated.
            if _looks_like_product_code_line(text):
                if any(_looks_like_product_code_line(str(l.get("text", "")).strip()) for l in groups[-1]):
                    groups.append([line])
                    continue
            # Booker purchase invoices use a leading numeric product code as the row anchor.
            # When OCR places the next row's pack/qty/price tokens slightly higher than its code,
            # they can drift into the previous row. Treat a second token in the same structural
            # data column as the start of a new row rather than absorbing it.
            if column in {"pack_size", "qty", "price", "value", "vat", "rrp_por"}:
                existing_items = [
                    existing for existing in groups[-1] if _assign_column(_mid_x(existing), ranges) == column
                ]
                if column == "rrp_por" and existing_items:
                    existing_text = " ".join(str(item.get("text") or "").strip() for item in existing_items).strip()
                    if (
                        (_looks_like_rrp_part(existing_text) and _looks_like_por_part(text))
                        or (_looks_like_por_part(existing_text) and _looks_like_rrp_part(text))
                    ):
                        groups[-1].append(line)
                        continue
                if existing_items:
                    groups.append([line])
                    continue
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def _row_has_item_signal(row: Dict[str, Any]) -> bool:
    code = str(row.get("code") or "").strip()
    description = str(row.get("description") or "").strip()
    qty = str(row.get("qty") or "").strip()
    price = str(row.get("price") or "").strip()
    value = str(row.get("value") or "").strip()
    if re.match(r"^\d{5,}$", code):
        return True
    if code and description:
        return True
    return any([qty, price, value])


def _looks_like_non_product_row(row: Dict[str, Any]) -> bool:
    merged = " ".join(
        str(row.get(key) or "").strip()
        for key in ("code", "description", "pack_size", "qty", "price", "value", "vat", "rrp_por")
    ).upper()
    merged = re.sub(r"\s+", " ", merged).strip()

    if not merged:
        return True

    blocked_phrases = [
        "SUB-TOTAL",
        "TOTAL ITEMS",
        "NETT",
        "INVOICE TOTAL",
        "PLEASE DO NOT BRING BACK",
        "EMPTY BREAD TRAY",
        "TRAYS AND KEGS",
        "GOODS :",
        "GOODS:",
        "EXC.VAT",
        "/CONT",
        "RATE",
        "SAVING DETAILS",
        "PROMOTIONS SAVED YOU",
        "TOTAL SAVINGS",
    ]
    if any(phrase in merged for phrase in blocked_phrases):
        return True

    if merged in {
        "RETAIL GROCERY",
        "CHILLED",
        "WINES SPIRITS BEERS",
        "CONFECTIONERY",
        "FROZEN",
        "TOBACCO",
    }:
        return True

    code = str(row.get("code") or "").strip().upper()
    desc = str(row.get("description") or "").strip().upper()
    pack_size = str(row.get("pack_size") or "").strip().upper()
    qty = str(row.get("qty") or "").strip().upper()
    price = str(row.get("price") or "").strip().upper()
    value = str(row.get("value") or "").strip().upper()
    vat = str(row.get("vat") or "").strip().upper()

    # Booker summary blocks can align into item columns as:
    # pack_size=ITEMS, qty=3, price=GOODS, value=29.93, vat=: ...
    # After orphan carry-forward, these rows may already have inherited a valid code
    # from the previous product row, so this check must not depend on code validity.
    if pack_size in {"ITEMS", "TOTAL ITEMS", "SUB-TOTAL"}:
        return True
    if price == "GOODS":
        return True
    if value in {"GOODS", "EXC.VAT", "EXC. VAT"}:
        return True
    if vat in {":", "EXC.VAT", "EXC. VAT"} and (
        pack_size in {"ITEMS", "TOTAL ITEMS", "SUB-TOTAL"} or price == "GOODS" or value
    ):
        return True

    if code.endswith(":") or code in {"RATE", "A: 0.00", "B:20.00", "TOTAL ITEMS:"}:
        return True

    if desc in {"RETAIL GROCERY", "WINES SPIRITS BEERS", ""} and not re.match(r"^\d{5,}$", code):
        if not str(row.get("price") or "").strip() and not str(row.get("value") or "").strip():
            return True

    if qty and not re.match(r"^-?\d+([.]\d+)?$", qty):
        if qty not in {"1-", "2-", "3-"}:
            # OCR junk / labels ending up in qty column are not product rows.
            if not re.match(r"^\d{5,}$", code):
                return True

    if not re.match(r"^\d{5,}$", code):
        # Exception: if the row has numeric qty and price/value but no code, OCR likely
        # missed the code line — preserve the data rather than discarding the row.
        if re.match(r"^-?\d+([.]\d+)?(-)?$", qty) and (
            str(row.get("value") or "").strip() or str(row.get("price") or "").strip()
        ):
            return False
        # Genuine product rows in this invoice family reliably carry a numeric item code.
        return True

    return False


def _split_code_description(text: str) -> Tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    match = re.match(r"^(\d{5,})\s+(.*)$", cleaned)
    if match:
        return match.group(1), match.group(2).strip()
    return "", cleaned


def _split_embedded_product_code_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Treat embedded Booker product codes as hard row boundaries.

    Tilted scans can merge the next row's code/description into the current
    description column, for example:
      302118 Fanta Orange PM140 302119 Fanta Fruit Twist PM140

    The numeric columns are still row-level OCR data, so this repair preserves
    the captured numeric payload and creates a separate row for each embedded
    product identity. Footer validation/scoring then chooses this candidate when
    it fixes the case and goods totals.
    """
    split_rows: List[Dict[str, Any]] = []
    code_re = re.compile(r"\b(\d{5,7})\b")
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        desc = str(row.get("description") or "").strip()
        matches = list(code_re.finditer(desc))
        split_at = None
        for match in matches:
            before = desc[: match.start()].strip()
            after = desc[match.end() :].strip()
            if before and after:
                split_at = match.start()
                break
        if split_at is None:
            split_rows.append(row)
            continue

        base = dict(row)
        base["description"] = desc[:split_at].strip()
        base["_embedded_code_split_source"] = "base"
        split_rows.append(base)

        tail = desc[split_at:].strip()
        tail_matches = list(code_re.finditer(tail))
        for idx, match in enumerate(tail_matches):
            code = match.group(1)
            seg_start = match.end()
            seg_end = tail_matches[idx + 1].start() if idx + 1 < len(tail_matches) else len(tail)
            seg_desc = tail[seg_start:seg_end].strip()
            if not seg_desc:
                continue
            clone = dict(row)
            clone["code"] = code
            clone["description"] = seg_desc
            clone["_embedded_code_split_source"] = str(row.get("code") or "")
            split_rows.append(clone)
    return split_rows


def _split_value_vat(value_text: str, vat_text: str) -> Tuple[str, str]:
    value_clean = re.sub(r"\s+", " ", str(value_text or "").strip())
    vat_clean = re.sub(r"\s+", " ", str(vat_text or "").strip())
    if vat_clean:
        return value_clean, vat_clean
    match = re.match(r"^(.*\S)\s+([ABZR])$", value_clean, re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2).upper()
    return value_clean, vat_clean


def _row_has_identity(row: Dict[str, Any]) -> bool:
    return bool(str(row.get("code") or "").strip() or str(row.get("description") or "").strip())


def _row_has_numeric_payload(row: Dict[str, Any]) -> bool:
    qty = str(row.get("qty") or "").strip()
    has_qty = bool(re.match(r"^-?\d+([.]\d+)?-?$", qty))
    has_value = bool(str(row.get("value") or "").strip())
    has_price = bool(str(row.get("price") or "").strip())
    return has_qty and (has_value or has_price)


def _rebalance_orphan_rows(rows: List[Dict[str, Any]]) -> None:
    # Booker OCR can emit numeric payload first and the product code/description on the
    # following row. Pull the identity backward so each numeric row gets the right code.
    for idx in range(len(rows) - 1):
        current = rows[idx]
        following = rows[idx + 1]
        if _row_has_identity(current) or not _row_has_numeric_payload(current):
            continue
        if not _row_has_identity(following):
            continue
        current["code"] = str(following.get("code") or "").strip()
        current["description"] = str(following.get("description") or "").strip()
        following["code"] = ""
        following["description"] = ""


def _is_right_edge_fragment_row(row: Dict[str, Any]) -> bool:
    if _row_has_identity(row):
        return False
    if str(row.get("pack_size") or "").strip():
        return False
    return any(str(row.get(key) or "").strip() for key in ("qty", "price", "value", "vat", "rrp_por"))


def _merge_fragment_row_into_previous(prev: Dict[str, Any], frag: Dict[str, Any]) -> bool:
    if not _row_has_identity(prev) or not _is_right_edge_fragment_row(frag):
        return False

    merged = False
    for key in ("qty", "price", "value", "vat", "rrp_por"):
        prev_value = str(prev.get(key) or "").strip()
        frag_value = str(frag.get(key) or "").strip()
        if not frag_value:
            continue
        if not prev_value:
            prev[key] = frag_value
            merged = True
            continue
        if key == "rrp_por":
            joined = " ".join(part for part in [prev_value, frag_value] if part).strip()
            if joined != prev_value:
                prev[key] = joined
                merged = True
    return merged


def _rebalance_right_edge_fragments(rows: List[Dict[str, Any]]) -> None:
    for idx in range(len(rows) - 1):
        current = rows[idx]
        following = rows[idx + 1]
        if not _is_right_edge_fragment_row(current) or not _row_has_identity(following):
            continue
        current_pack = str(current.get("pack_size") or "").strip()
        current_qty = str(current.get("qty") or "").strip()
        current_price = str(current.get("price") or "").strip()
        current_vat = str(current.get("vat") or "").strip()
        current_rrp = str(current.get("rrp_por") or "").strip()
        current_value = str(current.get("value") or "").strip()
        if current_pack or current_qty or current_price:
            continue
        if not (current_rrp or current_vat or _looks_like_vat_code(current_value)):
            continue
        for key in ("value", "vat", "rrp_por"):
            cur_value = str(current.get(key) or "").strip()
            next_value = str(following.get(key) or "").strip()
            if cur_value and not next_value:
                following[key] = cur_value
                current[key] = ""


def _rebalance_misaligned_rrp_por(rows: List[Dict[str, Any]]) -> None:
    for idx in range(1, len(rows)):
        current = rows[idx]
        previous = rows[idx - 1]
        current_rrp_por = str(current.get("rrp_por") or "").strip()
        previous_rrp_por = str(previous.get("rrp_por") or "").strip()
        if not current_rrp_por or previous_rrp_por:
            continue

        current_rrp = _extract_money_token(current_rrp_por)
        if not current_rrp:
            continue

        previous_expected = _infer_pmp_from_description(previous.get("description"))
        current_expected = _infer_pmp_from_description(current.get("description"))
        if not previous_expected or previous_expected != current_rrp:
            continue
        if current_expected and current_expected == current_rrp:
            continue

        previous["rrp_por"] = current_rrp_por
        current["rrp_por"] = ""


def _rebalance_forward_shifted_rrp_por(rows: List[Dict[str, Any]]) -> None:
    """Move rrp_por forward when OCR placed the next item's RRP/POR on the current row.

    This is the mirror of _rebalance_misaligned_rrp_por (which handles backward shift).
    Trigger: current row has rrp_por whose leading money value matches the PMP price of
    the *following* item — not the current one — and the following row has no rrp_por yet.
    """
    for idx in range(len(rows) - 1):
        current = rows[idx]
        following = rows[idx + 1]
        cur_rrp_por = str(current.get("rrp_por") or "").strip()
        fol_rrp_por = str(following.get("rrp_por") or "").strip()
        if not cur_rrp_por or fol_rrp_por:
            continue
        cur_rrp = _extract_money_token(cur_rrp_por)
        if not cur_rrp:
            continue
        cur_pmp = _infer_pmp_from_description(str(current.get("description") or ""))
        fol_pmp = _infer_pmp_from_description(str(following.get("description") or ""))
        # The captured RRP matches the following item's PMP price but not the current item's.
        if fol_pmp and fol_pmp == cur_rrp and (not cur_pmp or cur_pmp != cur_rrp):
            following["rrp_por"] = cur_rrp_por
            current["rrp_por"] = ""


def _normalize_booker_right_edge(row: Dict[str, Any]) -> None:
    row["value"], row["vat"] = _split_value_vat(row.get("value"), row.get("vat"))

    value = str(row.get("value") or "").strip()
    vat = str(row.get("vat") or "").strip()
    rrp_por = str(row.get("rrp_por") or "").strip()
    price = str(row.get("price") or "").strip()
    qty = str(row.get("qty") or "").strip()

    trailing_vat_match = re.match(r"^(.*\S)\s+([ABZR])$", value, re.IGNORECASE)
    if trailing_vat_match and (not vat or _looks_like_rrp_por(vat)):
        value = trailing_vat_match.group(1).strip()
        trailing_code = trailing_vat_match.group(2).upper()
        if _looks_like_rrp_por(vat) and not rrp_por:
            rrp_por = vat
        vat = trailing_code

    # OCR can place the VAT code as a lone token in the rrp_por bucket while the
    # real Booker "Std RRP / POR" pair lands in the vat bucket.
    if _looks_like_vat_code(rrp_por) and _looks_like_rrp_por(vat):
        vat, rrp_por = rrp_por.upper(), vat

    # If a Booker rrp/por pair was mis-bucketed into vat and vat has no code,
    # preserve the pair in rrp_por instead.
    if not _looks_like_vat_code(vat) and _looks_like_rrp_por(vat):
        if not rrp_por:
            rrp_por = vat
        vat = ""

    # OCR can also place the VAT code into value/rrp_por while leaving vat blank.
    if not vat and _looks_like_vat_code(value):
        vat = value.upper()
        value = ""
    if not vat and _looks_like_vat_code(rrp_por):
        vat = rrp_por.upper()
        rrp_por = ""

    # Handle combined "VAT+RRP+POR" as a single OCR token in rrp_por (e.g. "B 1.25 41.2%")
    # This happens when OCR runs the letter, money value and percent together on one line.
    if not vat and rrp_por:
        m = re.match(r'^([ABZR])\s+(\d+\.\d+(?:\s+\d+\.\d+%)?)\s*$', rrp_por.strip(), re.IGNORECASE)
        if m:
            vat = m.group(1).upper()
            rrp_por = m.group(2).strip()
    # Same combined token may land in the vat bucket instead of rrp_por.
    if vat and not _looks_like_vat_code(vat):
        m = re.match(r'^([ABZR])\s+(\d+\.\d+(?:\s+\d+\.\d+%)?)\s*$', vat.strip(), re.IGNORECASE)
        if m:
            extracted_vat = m.group(1).upper()
            extracted_rrp = m.group(2).strip()
            vat = extracted_vat
            if not rrp_por:
                rrp_por = extracted_rrp

    # Recover obvious missing totals/prices from neighboring numeric fields.
    if not value:
        inferred_value = _infer_value_from_price_qty(price, qty)
        if inferred_value:
            value = inferred_value
    if not price:
        inferred_price = _infer_price_from_value_qty(value, qty)
        if inferred_price:
            price = inferred_price

    row["price"] = price
    row["value"] = value
    row["vat"] = vat
    row["rrp_por"] = rrp_por


def _extract_rows_for_page(payload: Dict[str, Any], page: int) -> List[Dict[str, Any]]:
    page_lines = _page_lines(payload, page)
    headers = _find_column_headers(page_lines)
    if not headers:
        return []

    header_y = min(_mid_y(line) for line in headers.values())
    ranges = _column_ranges(headers)
    content_lines = [line for line in page_lines if not _is_footer_or_noise(line, header_y)]
    grouped = _group_rows(content_lines, ranges)

    parsed_rows: List[Dict[str, Any]] = []
    for group in grouped:
        values: Dict[str, List[str]] = {name: [] for name in COLUMN_ORDER}
        for line in sorted(group, key=_left):
            column = _assign_column(_mid_x(line), ranges)
            values[column].append(str(line.get("text") or "").strip())

        row = {
            "page": page,
            "row_y": round(sum(_mid_y(item) for item in group) / len(group), 6),
            "code": "",
            "description": "",
            "pack_size": " ".join(values["pack_size"]).strip(),
            "qty": " ".join(values["qty"]).strip(),
            "price": " ".join(values["price"]).strip(),
            "value": " ".join(values["value"]).strip(),
            "vat": " ".join(values["vat"]).strip(),
            "rrp_por": " ".join(values["rrp_por"]).strip(),
        }

        code_text = " ".join(values["code"]).strip()
        desc_text = " ".join(values["description"]).strip()
        code_from_code, desc_from_code = _split_code_description(code_text)
        if code_from_code:
            row["code"] = code_from_code
            row["description"] = " ".join(part for part in [desc_from_code, desc_text] if part).strip()
        else:
            row["code"] = code_text
            row["description"] = desc_text

        if not row["code"]:
            code_from_desc, desc_from_desc = _split_code_description(row["description"])
            if code_from_desc:
                row["code"] = code_from_desc
                row["description"] = desc_from_desc

        row["value"], row["vat"] = _split_value_vat(row["value"], row["vat"])
        parsed_rows.append(row)

    _rebalance_orphan_rows(parsed_rows)
    _rebalance_right_edge_fragments(parsed_rows)
    _rebalance_misaligned_rrp_por(parsed_rows)
    _rebalance_forward_shifted_rrp_por(parsed_rows)
    parsed_rows = _split_embedded_product_code_rows(parsed_rows)

    rows: List[Dict[str, Any]] = []
    for row in parsed_rows:
        if _is_right_edge_fragment_row(row) and rows:
            if _merge_fragment_row_into_previous(rows[-1], row):
                _normalize_booker_right_edge(rows[-1])
                continue

        _normalize_booker_right_edge(row)

        # Orphan: no code, no description, but has pack_size.
        # Only merge into the previous row when it carries no numeric data (a text fragment).
        # If it has qty and price/value, it is a product row whose code OCR missed — preserve it.
        _orphan_qty = str(row.get("qty") or "").strip()
        _is_data_orphan = bool(re.match(r"^-?\d", _orphan_qty)) and bool(
            str(row.get("value") or "").strip() or str(row.get("price") or "").strip()
        )
        if (
            not row["description"]
            and row["pack_size"]
            and not row["code"]
            and rows
            and not _is_data_orphan
            and not _looks_like_non_product_row(row)
        ):
            prev = rows[-1]
            for key in ("pack_size", "qty", "price", "value", "vat", "rrp_por"):
                if row.get(key):
                    prev[key] = " ".join(part for part in [str(prev.get(key) or "").strip(), str(row[key]).strip()] if part).strip()
            continue

        # Carry-forward: if OCR missed the code/description on a row that has valid
        # numeric data, inherit them from the immediately preceding product row so the
        # item appears with the correct identity rather than blank code and description.
        # Do not carry-forward onto fully blank rows, or they become synthetic duplicates.
        if not row["code"] and not row["description"] and rows and _row_has_numeric_payload(row):
            prev = rows[-1]
            if prev.get("code"):
                row["code"] = prev["code"]
                row["description"] = prev.get("description", "")

        if _row_has_item_signal(row) and not _looks_like_non_product_row(row):
            rows.append(row)

    return rows


def extract_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    pages = sorted({int(line.get("page") or 1) for line in list(payload.get("ocr_lines") or [])})
    all_rows: List[Dict[str, Any]] = []
    for page in pages:
        all_rows.extend(_extract_rows_for_page(payload, page))
    for idx, row in enumerate(all_rows, start=1):
        row["line_no"] = idx
    return all_rows


def _print_rows(rows: List[Dict[str, Any]]) -> None:
    headers = ["line_no", "page", "code", "description", "pack_size", "qty", "price", "value", "vat", "rrp_por"]
    widths = {header: len(header) for header in headers}
    rendered: List[Dict[str, str]] = []
    for row in rows:
        rendered_row = {header: str(row.get(header) or "") for header in headers}
        rendered.append(rendered_row)
        for header in headers:
            widths[header] = max(widths[header], min(len(rendered_row[header]), 60))

    def trunc(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        if width <= 3:
            return value[:width]
        return value[: width - 3] + "..."

    def fmt(row: Dict[str, str]) -> str:
        return " | ".join(trunc(row[h], widths[h]).ljust(widths[h]) for h in headers)

    import sys

    def safe_print(text: str) -> None:
        try:
            print(text)
        except UnicodeEncodeError:
            print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8", errors="replace"))

    safe_print(fmt({h: h for h in headers}))
    safe_print("-+-".join("-" * widths[h] for h in headers))
    for row in rendered:
        safe_print(fmt(row))


def _write_csv(rows: List[Dict[str, Any]], csv_path: Path) -> None:
    fields = ["line_no", "page", "code", "description", "pack_size", "qty", "price", "value", "vat", "rrp_por", "row_y"]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract line-item rows with column names and values from Document AI OCR JSON.",
    )
    parser.add_argument("ocr_json", help="Path to the OCR JSON file.")
    parser.add_argument("--csv", default="", help="Optional CSV output path.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    json_path = Path(args.ocr_json).expanduser().resolve()
    if not json_path.is_file():
        parser.error(f"OCR JSON file not found: {json_path}")

    rows = extract_rows(_load_payload(json_path))
    _print_rows(rows)
    if args.csv:
        csv_path = Path(args.csv).expanduser().resolve()
        _write_csv(rows, csv_path)
        print(f"\nWrote CSV to {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

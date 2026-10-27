from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from extract_dhamecha_items_table import extract_rows as extract_dhamecha_rows
from extract_documentai_items_table import extract_rows as extract_booker_purchase_rows


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


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()


def _ascii_safe(value: str) -> str:
    return str(value).encode("ascii", "replace").decode("ascii")


_PMP_RE = re.compile(r"(?<!\d)PMP?\s*(\d{2,4})\b", re.IGNORECASE)
_PACK_SIZE_SPLIT_RE = re.compile(r"^\s*(\d+)\s*(.*)$")
_RRP_POR_SPLIT_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)?\s*([0-9]+(?:\.[0-9]+)?%)?\s*$")
_VAT_RATE_RE = re.compile(r"\b([A-Z])\s*:\s*([0-9]+(?:\.[0-9]+)?)\b")
_BOOKER_PRODUCT_CODE_RE = re.compile(r"^\s*(\d{5,7})\s+(.+?)\s*$")
_BOOKER_PACK_RE = re.compile(r"^\s*(\d{1,3})\s+([0-9.]+(?:ML|M1|L|LTR|LT|CL|G|KG|PK|PCK|PACK|ROLLS?|S)\b.*)$", re.IGNORECASE)
_BOOKER_MONEY_RE = re.compile(r"-?\d+(?:[.,:]\d{2})-?")


def _infer_pmp_from_description(description: Any) -> str:
    text = str(description or "")
    match = _PMP_RE.search(text)
    if not match:
        return ""
    try:
        return f"{int(match.group(1)) / 100:.2f}"
    except Exception:
        return ""


def _split_pack_size(pack_size: Any) -> Tuple[str, str]:
    text = _clean_text(pack_size)
    if not text:
        return "", ""
    match = _PACK_SIZE_SPLIT_RE.match(text)
    if not match:
        return "", text
    case_pack = match.group(1).strip()
    unit_size = match.group(2).strip()
    return case_pack, unit_size


def _split_rrp_por(rrp_por: Any) -> Tuple[str, str]:
    text = _clean_text(rrp_por)
    if not text:
        return "", ""
    match = _RRP_POR_SPLIT_RE.match(text)
    if match:
        return (match.group(1) or "").strip(), (match.group(2) or "").strip()
    parts = text.split()
    if len(parts) >= 2:
        return parts[0].strip(), " ".join(parts[1:]).strip()
    return text, ""


def _infer_total_units(case_pack: Any, quantity: Any) -> str:
    try:
        case_value = float(str(case_pack).strip())
        qty_text = _clean_text(quantity)
        qty_match = re.match(r"^-?\d+(?:\.\d+)?", qty_text)
        if not qty_match:
            return ""
        qty_value = float(qty_match.group(0))
        total_units = case_value * qty_value
        if total_units.is_integer():
            return str(int(total_units))
        return f"{total_units:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return ""


def _extract_vat_rate_map(payload: Dict[str, Any]) -> Dict[str, str]:
    hay = " ".join(
        [
            str(payload.get("raw_text") or ""),
            str(payload.get("line_text") or ""),
        ]
    ).upper()
    vat_rates = {"A": "0.00", "B": "20.00", "C": "5.00"}
    for code, rate in _VAT_RATE_RE.findall(hay):
        vat_rates[code.strip().upper()] = rate.strip()
    return vat_rates


def _infer_vat_percent(vat_code: Any, vat_rates: Dict[str, str]) -> str:
    text = _clean_text(vat_code).upper()
    if not text:
        return ""
    match = re.search(r"[A-Z]", text)
    if not match:
        return ""
    return vat_rates.get(match.group(0), "")


def _parse_signed_number(value: Any) -> Optional[float]:
    text = _clean_text(value)
    if not text:
        return None
    sign = -1.0 if text.endswith("-") else 1.0
    text = text.rstrip("-").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return sign * float(match.group(0))
    except Exception:
        return None


def _booker_money_token(value: Any) -> str:
    text = _clean_text(value).replace(",", ".").replace(":", ".")
    match = _BOOKER_MONEY_RE.search(text)
    return match.group(0) if match else ""


def _booker_parse_qty_text(value: Any) -> str:
    text = _clean_text(value)
    if re.match(r"^-?\d+(?:\.\d+)?-?$", text):
        return text
    match = re.search(r"-?\d+(?:\.\d+)?-?", text)
    if match:
        return match.group(0)
    # OCR often reads a single-case quantity as L/I/| or short junk such as ds/uF.
    if text and len(text) <= 3 and re.search(r"[A-Za-z|]", text):
        return "1"
    return ""


def _booker_split_value_vat_text(value_text: str, following_text: str = "") -> Tuple[str, str]:
    value_clean = _clean_text(value_text).replace(",", ".").replace(":", ".")
    match = re.match(r"^(.*?\d+(?:\.\d{2})-?)\s+([ABZRC])\b", value_clean, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip(), match.group(2).upper()
    vat = _clean_text(following_text).upper()
    if re.fullmatch(r"[ABZRC]", vat):
        return value_clean, vat
    return value_clean, ""


def _booker_row_from_single_text_line(line: str, line_no: int) -> Optional[Dict[str, Any]]:
    text = _clean_text(line).replace(",", ".").replace(":", ".")
    match = _BOOKER_PRODUCT_CODE_RE.match(text)
    if not match:
        return None
    toks = text.split()
    if len(toks) < 8:
        return None
    code = re.sub(r"\D+", "", toks[0])

    vat_idx = None
    for idx in range(len(toks) - 1, 0, -1):
        vat = re.sub(r"[^A-Z]+", "", toks[idx].upper())
        if vat in {"A", "B", "C", "Z", "R"}:
            vat_idx = idx
            break
    if vat_idx is None:
        return None

    value_idx = None
    for idx in range(vat_idx - 1, 1, -1):
        token = toks[idx].replace(",", ".").replace(":", ".")
        if re.fullmatch(r"\d{3,5}", token):
            # OCR sometimes drops the decimal: 6225 -> 6.25.
            token = f"{token[:-2]}.{token[-2:]}"
            toks[idx] = token
        if _BOOKER_MONEY_RE.fullmatch(token):
            value_idx = idx
            break
    if value_idx is None:
        return None

    price_idx = None
    scan_idx = value_idx - 1
    if scan_idx > 1 and toks[scan_idx].upper() in {"P", "SL"}:
        scan_idx -= 1
    for idx in range(scan_idx, 1, -1):
        token = toks[idx].replace(",", ".").replace(":", ".")
        if _BOOKER_MONEY_RE.fullmatch(token):
            price_idx = idx
            break
    if price_idx is None:
        return None

    qty_idx = None
    for idx in range(price_idx - 1, 1, -1):
        qty = _booker_parse_qty_text(toks[idx])
        if qty:
            qty_idx = idx
            toks[idx] = qty
            break
    if qty_idx is None or qty_idx - 2 < 1:
        return None

    case_pack = toks[qty_idx - 2]
    unit_size = toks[qty_idx - 1]
    if not re.fullmatch(r"\d{1,3}", case_pack):
        return None

    value, vat = _booker_split_value_vat_text(" ".join(toks[value_idx : vat_idx + 1]))
    value = _booker_money_token(value) or toks[value_idx]
    rrp_por = " ".join(toks[vat_idx + 1 :]).strip()
    return {
        "line_no": line_no,
        "page": 1,
        "code": code,
        "description": " ".join(toks[1 : qty_idx - 2]).strip(),
        "pack_size": f"{case_pack} {unit_size}",
        "qty": toks[qty_idx],
        "price": " ".join(toks[price_idx:value_idx]).strip(),
        "value": value,
        "vat": vat,
        "rrp_por": rrp_por,
        "row_y": float(line_no),
    }


def _booker_rows_from_raw_text(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse Booker stacked raw_text rows using product-code anchors.

    This is a repair source for skewed scans where OCR row boxes merge adjacent
    product rows, but raw_text still lists each product code as its own block.
    """
    raw = str((payload or {}).get("raw_text") or "")
    if not raw:
        return []
    lines = [_clean_text(line) for line in raw.splitlines()]
    lines = [line for line in lines if line]
    rows: List[Dict[str, Any]] = []
    stop_words = ("SUB-TOTAL", "TOTAL ITEMS", "INVOICE TOTAL", "TOTALS:", "PAYMENT ", "PLEASE ")

    idx = 0
    while idx < len(lines):
        line = lines[idx]
        upper = line.upper()
        if any(word in upper for word in stop_words):
            idx += 1
            continue
        single_row = _booker_row_from_single_text_line(line, len(rows) + 1)
        if single_row:
            rows.append(single_row)
            idx += 1
            continue
        match = _BOOKER_PRODUCT_CODE_RE.match(line)
        if not match:
            idx += 1
            continue

        code = match.group(1)
        description = match.group(2).strip()
        if not description or any(word in description.upper() for word in ("BOOKER", "CUSTOMER", "INVOICE")):
            idx += 1
            continue

        cursor = idx + 1
        if cursor >= len(lines):
            break
        pack_line = lines[cursor]
        pack_match = _BOOKER_PACK_RE.match(pack_line)
        if not pack_match:
            idx += 1
            continue
        pack_size = f"{pack_match.group(1)} {pack_match.group(2)}".strip()
        cursor += 1

        if cursor + 2 >= len(lines):
            break
        qty = _booker_parse_qty_text(lines[cursor])
        price = _clean_text(lines[cursor + 1]).replace(",", ".").replace(":", ".")
        value_text = _clean_text(lines[cursor + 2]).replace(",", ".").replace(":", ".")
        if not qty or not _booker_money_token(price):
            idx += 1
            continue

        value = _booker_money_token(value_text)
        following = lines[cursor + 3] if cursor + 3 < len(lines) else ""
        value, vat = _booker_split_value_vat_text(value_text, following)
        value = _booker_money_token(value)
        if not value:
            idx += 1
            continue

        rrp_por = ""
        next_after_value = cursor + 3
        if vat and next_after_value < len(lines):
            rrp_por = lines[next_after_value]
        elif not vat and next_after_value + 1 < len(lines):
            rrp_por = lines[next_after_value + 1]

        rows.append(
            {
                "line_no": len(rows) + 1,
                "page": 1,
                "code": code,
                "description": description,
                "pack_size": pack_size,
                "qty": qty,
                "price": price,
                "value": value,
                "vat": vat,
                "rrp_por": rrp_por if re.search(r"\d", rrp_por or "") else "",
                "row_y": float(len(rows) + 1),
            }
        )
        idx = cursor + 3

    return rows


def _infer_vat_amount(total_net: Any, vat_percent: Any) -> str:
    total_value = _parse_signed_number(total_net)
    rate_value = _parse_signed_number(vat_percent)
    if total_value is None or rate_value is None:
        return ""
    vat_amount = total_value * (rate_value / 100.0)
    return f"{vat_amount:.2f}"


def _infer_gross_amount(total_net: Any, vat_amount: Any) -> str:
    total_value = _parse_signed_number(total_net)
    vat_value = _parse_signed_number(vat_amount)
    if total_value is None or vat_value is None:
        return ""
    gross_value = total_value + vat_value
    return f"{gross_value:.2f}"


def _enrich_common_columns(rows: List[Dict[str, Any]], vat_rates: Dict[str, str]) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        pmp_value = _infer_pmp_from_description(row.get("description"))
        case_pack, unit_size = _split_pack_size(row.get("pack_size"))
        std_rrp, por = _split_rrp_por(row.get("rrp_por"))
        total_units = _infer_total_units(case_pack, row.get("qty"))
        vat_percent = _infer_vat_percent(row.get("vat"), vat_rates)
        vat_amount = _infer_vat_amount(row.get("value"), vat_percent)
        gross_amount = _infer_gross_amount(row.get("value"), vat_amount)
        reordered: Dict[str, Any] = {}
        description_inserted = False
        pack_columns_inserted = False
        rrp_por_inserted = False
        for key, value in row.items():
            if key == "description":
                reordered[key] = value
                reordered["pmp"] = pmp_value
                description_inserted = True
                continue
            if key == "pack_size":
                reordered["Case Pack"] = case_pack
                reordered["unit_size"] = unit_size
                pack_columns_inserted = True
                continue
            if key == "qty":
                reordered[key] = value
                reordered["Total units"] = total_units
                continue
            if key == "vat":
                reordered[key] = value
                reordered["VAT %"] = vat_percent
                reordered["VAT Amount"] = vat_amount
                reordered["Gross"] = gross_amount
                continue
            if key == "rrp_por":
                reordered["Std RRP"] = std_rrp
                reordered["por"] = por
                rrp_por_inserted = True
                continue
            reordered[key] = value
        if not description_inserted:
            reordered["pmp"] = pmp_value
        if not pack_columns_inserted:
            reordered["Case Pack"] = case_pack
            reordered["unit_size"] = unit_size
        if not rrp_por_inserted:
            reordered["Std RRP"] = std_rrp
            reordered["por"] = por
        enriched.append(reordered)
    return enriched


def _rename_output_columns(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    renamed_rows: List[Dict[str, Any]] = []
    column_map = {
        "qty": "Quantity",
        "price": "Unit Price",
        "value": "Total/Net",
        "vat": "VAT code",
        "unit_size": "Unit Size",
    }
    for row in rows:
        renamed: Dict[str, Any] = {}
        for key, value in row.items():
            renamed[column_map.get(key, key)] = value
        renamed_rows.append(renamed)
    return renamed_rows


def detect_supplier_family(payload: Dict[str, Any]) -> str:
    hay = " ".join(
        [
            str(payload.get("source_file") or ""),
            str(payload.get("raw_text") or ""),
            str(payload.get("line_text") or ""),
        ]
    ).upper()
    if "BOOKER" in hay:
        return "BOOKER"
    if "DHAMECHA" in hay:
        return "DHAMECHA"
    if "PARFETT" in hay or "PARFETS" in hay or "PAFFET" in hay:
        return "PARFETTS"
    return "GENERIC"


def _detect_booker_layout(payload: Dict[str, Any]) -> str:
    hay = " ".join(
        [
            str(payload.get("raw_text") or ""),
            str(payload.get("line_text") or ""),
        ]
    ).upper()
    if "RETAIL PARTNERS" in hay or ("QTY VAT" in hay and "NET VALUE" in hay and "PRD. CODE" in hay):
        return "BOOKER_DELIVERY"
    return "BOOKER_PURCHASE"


def _line_mid_x(line: Dict[str, Any]) -> float:
    bbox = _bbox_dict(line)
    return (_safe_float(bbox.get("left")) + _safe_float(bbox.get("right"))) / 2.0


def _line_mid_y(line: Dict[str, Any]) -> float:
    bbox = _bbox_dict(line)
    return (_safe_float(bbox.get("top")) + _safe_float(bbox.get("bottom"))) / 2.0


def _line_left(line: Dict[str, Any]) -> float:
    return _safe_float(_bbox_dict(line).get("left"))


GENERIC_COLUMN_ORDER = [
    "code",
    "description",
    "pack_size",
    "qty",
    "price",
    "value",
    "vat",
    "rrp_por",
]

HEADER_ALIASES = {
    "code": {"code", "item #", "item#", "item no", "item number", "item", "sku", "plu"},
    "description": {"product description", "description", "product", "item description", "desc"},
    "pack_size": {"pack size", "pack", "size", "uop", "unit size"},
    "qty": {"qty", "quantity"},
    "price": {"price", "price(£)", "price(?)", "unit price", "uos", "uop uos qty price(£)"},
    "value": {"ext.price(£)", "ext.price(?)", "ext price", "value", "total", "net", "ext.price"},
    "vat": {"vat", "vat code"},
    "rrp_por": {"rrp(£)", "rrp(?)", "rrp por", "rrp", "por"},
}


def _normalise_header_token(text: str) -> str:
    return re.sub(r"\s+", " ", text.upper()).strip()


def _find_generic_headers(page_lines: List[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, Any]]]:
    headers: Dict[str, Dict[str, Any]] = {}
    for line in page_lines:
        text = _normalise_header_token(str(line.get("text") or ""))
        for field, aliases in HEADER_ALIASES.items():
            if text in {alias.upper() for alias in aliases}:
                headers[field] = line
                break
    if len(headers) < 5:
        return None
    return headers


def _column_ranges(headers: Dict[str, Dict[str, Any]]) -> Dict[str, Tuple[float, float]]:
    centers = [(name, _line_mid_x(line)) for name, line in headers.items() if name in GENERIC_COLUMN_ORDER]
    centers.sort(key=lambda item: item[1])
    if not centers:
        return {}
    boundaries: List[float] = [0.0]
    for idx in range(len(centers) - 1):
        boundaries.append((centers[idx][1] + centers[idx + 1][1]) / 2.0)
    boundaries.append(1.0)
    ranges: Dict[str, Tuple[float, float]] = {}
    for idx, (name, _) in enumerate(centers):
        ranges[name] = (boundaries[idx], boundaries[idx + 1])
    return ranges


def _assign_column(x_center: float, ranges: Dict[str, Tuple[float, float]]) -> str:
    for name in GENERIC_COLUMN_ORDER:
        if name not in ranges:
            continue
        left, right = ranges[name]
        if left <= x_center < right:
            return name
    return GENERIC_COLUMN_ORDER[-1]


def _is_header_or_footer(line: Dict[str, Any], header_y: float) -> bool:
    text = _normalise_header_token(str(line.get("text") or ""))
    if text in {alias.upper() for aliases in HEADER_ALIASES.values() for alias in aliases}:
        return True
    if _line_mid_y(line) <= header_y:
        return True
    blocked = (
        "TOTAL",
        "SUB TOTAL",
        "SUB-TOTAL",
        "INVOICE TOTAL",
        "THANK YOU",
        "PAGE ",
        "/CONT",
        "VAT SUMMARY",
    )
    return any(tok in text for tok in blocked)


def _group_rows(lines: List[Dict[str, Any]], tolerance: float = 0.0065) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    for line in sorted(lines, key=lambda item: (_line_mid_y(item), _line_left(item))):
        y = _line_mid_y(line)
        if not groups:
            groups.append([line])
            continue
        prev_y = sum(_line_mid_y(item) for item in groups[-1]) / len(groups[-1])
        if abs(y - prev_y) <= tolerance:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def _generic_rows_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    page_map: Dict[int, List[Dict[str, Any]]] = {}
    for line in list(payload.get("ocr_lines") or []):
        if not str(line.get("text") or "").strip():
            continue
        page_map.setdefault(int(line.get("page") or 1), []).append(line)

    header_page = None
    headers = None
    for page in sorted(page_map.keys()):
        headers = _find_generic_headers(page_map[page])
        if headers:
            header_page = page
            break
    if header_page is None or not headers:
        return []

    ranges = _column_ranges(headers)
    header_y = min(_line_mid_y(line) for line in headers.values())
    rows: List[Dict[str, Any]] = []

    for page in sorted(page_map.keys()):
        if page < header_page:
            continue
        content_lines = [line for line in page_map[page] if not _is_header_or_footer(line, header_y)]
        for group in _group_rows(content_lines):
            fields: Dict[str, List[str]] = {name: [] for name in GENERIC_COLUMN_ORDER}
            for line in sorted(group, key=_line_left):
                column = _assign_column(_line_mid_x(line), ranges)
                fields[column].append(_clean_text(line.get("text")))

            row = {
                "line_no": len(rows) + 1,
                "page": page,
                "code": " ".join(fields["code"]).strip(),
                "description": " ".join(fields["description"]).strip(),
                "pack_size": " ".join(fields["pack_size"]).strip(),
                "qty": " ".join(fields["qty"]).strip(),
                "price": " ".join(fields["price"]).strip(),
                "value": " ".join(fields["value"]).strip(),
                "vat": " ".join(fields["vat"]).strip(),
                "rrp_por": " ".join(fields["rrp_por"]).strip(),
                "row_y": round(sum(_line_mid_y(line) for line in group) / len(group), 6),
            }
            if row["code"] or row["description"]:
                rows.append(row)
    return rows


_BOOKER_DELIVERY_STOP_TOKENS = (
    "REMITTANCE ADVICE",
    "TOTAL GOODS",
    "TOTAL NET VALUE",
    "TOTAL PAYABLE",
    "REGISTERED OFFICE:",
    "PAGE ",
)


_BOOKER_DELIVERY_CODE_RE = re.compile(r"^[A-Z]?\d{5,}$")
_BOOKER_DELIVERY_PACK_RE = re.compile(r"^\d+\s*[A-Z]*$")
_BOOKER_DELIVERY_MONEY_RE = re.compile(r"^\d+\.\d{2}$")
_BOOKER_DELIVERY_QTY_VAT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s+([A-Z]\d?)$")


def _booker_delivery_rows_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = [str(line).strip() for line in str(payload.get("raw_text") or "").splitlines()]
    lines = [line for line in lines if line]

    try:
        start = lines.index("DAIRY DELI & BAKERY") + 1
    except ValueError:
        start = 0
        for idx, line in enumerate(lines):
            upper = line.upper()
            if _BOOKER_DELIVERY_CODE_RE.match(upper):
                start = idx
                break

    rows: List[Dict[str, Any]] = []
    idx = start
    while idx < len(lines):
        token = lines[idx].strip()
        upper = token.upper()
        if any(upper.startswith(stop) for stop in _BOOKER_DELIVERY_STOP_TOKENS):
            break
        if not _BOOKER_DELIVERY_CODE_RE.match(upper):
            idx += 1
            continue

        row_code = token
        cursor = idx + 1
        if cursor < len(lines) and lines[cursor].strip().upper() in {"N/A", "NA"}:
            cursor += 1

        if cursor >= len(lines):
            break
        description = lines[cursor].strip()
        cursor += 1

        if cursor + 2 >= len(lines):
            break

        pack_size = lines[cursor].strip()
        price = lines[cursor + 1].strip()
        qty_vat = lines[cursor + 2].strip()
        value = lines[cursor + 3].strip() if cursor + 3 < len(lines) else ""

        qty = ""
        vat = ""
        qty_vat_match = _BOOKER_DELIVERY_QTY_VAT_RE.match(qty_vat.upper())
        if qty_vat_match:
            qty = qty_vat_match.group(1)
            vat = qty_vat_match.group(2)
        else:
            parts = qty_vat.split()
            if len(parts) >= 2:
                qty = parts[0]
                vat = parts[-1]

        if (
            _BOOKER_DELIVERY_PACK_RE.match(pack_size.upper())
            and _BOOKER_DELIVERY_MONEY_RE.match(price)
            and qty
            and vat
            and _BOOKER_DELIVERY_MONEY_RE.match(value)
        ):
            rows.append(
                {
                    "line_no": len(rows) + 1,
                    "page": 1,
                    "code": row_code,
                    "description": description,
                    "pack_size": pack_size,
                    "qty": qty,
                    "price": price,
                    "value": value,
                    "vat": vat,
                    "rrp_por": "",
                    "row_y": float(len(rows) + 1),
                }
            )
            idx = cursor + 4
            continue

        idx += 1

    return rows


def _booker_rows_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    layout = _detect_booker_layout(payload)
    if layout == "BOOKER_DELIVERY":
        return _booker_delivery_rows_from_payload(payload)
    # Booker purchase invoices use leading numeric item codes as row anchors.
    # The dedicated extractor applies the Booker-specific row grouping and
    # orphan-row rebalancing needed for OCR JSON from scanned invoices.
    rows = extract_booker_purchase_rows(payload)

    # Repair skewed/tilted scans where the OCR line boxes merge two adjacent
    # products into one row. In those cases raw_text often still has each code
    # as a separate stacked block, so add only the product codes missing from
    # the table extraction.
    raw_rows = _booker_rows_from_raw_text(payload)
    if not raw_rows:
        return rows
    existing_counts: Dict[str, int] = {}
    for row in rows:
        code = str(row.get("code") or "").strip()
        if code:
            existing_counts[code] = existing_counts.get(code, 0) + 1

    repaired = list(rows)
    for raw_row in raw_rows:
        code = str(raw_row.get("code") or "").strip()
        if not code:
            continue
        count = existing_counts.get(code, 0)
        if count > 0:
            existing_counts[code] = count - 1
            continue
        repaired.append(raw_row)

    repaired.sort(key=lambda row: (int(_safe_float(row.get("page")) or 1), float(_safe_float(row.get("row_y")) or 0.0), str(row.get("code") or "")))
    for idx, row in enumerate(repaired, start=1):
        row["line_no"] = idx
    return repaired


def build_rows(payload: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    supplier = detect_supplier_family(payload)
    vat_rates = _extract_vat_rate_map(payload)
    if supplier == "BOOKER":
        return supplier, _rename_output_columns(_enrich_common_columns(_booker_rows_from_payload(payload), vat_rates))
    if supplier == "DHAMECHA":
        return supplier, _rename_output_columns(_enrich_common_columns(extract_dhamecha_rows(payload), vat_rates))
    return supplier, _rename_output_columns(_enrich_common_columns(_generic_rows_from_payload(payload), vat_rates))


def _print_rows(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("No rows extracted.")
        return
    headers = list(rows[0].keys())
    widths = {header: len(header) for header in headers}
    rendered_rows: List[Dict[str, str]] = []

    for row in rows:
        rendered: Dict[str, str] = {}
        for header in headers:
            value = row.get(header, "")
            if isinstance(value, float):
                rendered[header] = f"{value:.2f}"
            else:
                rendered[header] = str(value)
            rendered[header] = _ascii_safe(rendered[header])
            widths[header] = max(widths[header], min(len(rendered[header]), 48))
        rendered_rows.append(rendered)

    def trunc(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        if width <= 3:
            return value[:width]
        return value[: width - 3] + "..."

    def fmt(row: Dict[str, str]) -> str:
        return " | ".join(trunc(row[h], widths[h]).ljust(widths[h]) for h in headers)

    print(fmt({header: header for header in headers}))
    print("-+-".join("-" * widths[header] for header in headers))
    for row in rendered_rows:
        print(fmt(row))


def _write_csv(rows: List[Dict[str, Any]], csv_path: Path) -> None:
    if not rows:
        csv_path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Common OCR JSON to line-item CSV extractor for purchase invoices.")
    parser.add_argument("ocr_json", help="Path to the OCR JSON file.")
    parser.add_argument("--csv", default="", help="Optional CSV output path.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    json_path = Path(args.ocr_json).expanduser().resolve()
    if not json_path.is_file():
        parser.error(f"OCR JSON file not found: {json_path}")

    payload = _load_payload(json_path)
    supplier, rows = build_rows(payload)
    print(f"Detected supplier/layout: {supplier}")
    _print_rows(rows)
    if args.csv:
        csv_path = Path(args.csv).expanduser().resolve()
        _write_csv(rows, csv_path)
        print(f"\nWrote CSV to {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

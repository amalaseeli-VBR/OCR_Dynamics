"""Dhamecha-specific reconciliation and VAT normalization."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from .parsers.dhamecha import looks_like_dhamecha_invoice


def _number(value: Any) -> float:
    try:
        cleaned = re.sub(r"[^0-9.\-]", "", str(value or "").replace(",", ""))
        return float(cleaned) if cleaned not in {"", "-", ".", "-."} else 0.0
    except (TypeError, ValueError):
        return 0.0


def _line_numbers(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for index, item in enumerate(items, 1):
        item["lineNo"] = index
    return items


def normalize_line_vat_for_reconciliation(
    items: List[Dict[str, Any]],
    header: Dict[str, Any],
    text: str = "",
) -> List[Dict[str, Any]]:
    """Force line VAT from Dhamecha's printed A/B/R/Z code."""
    supplier_hint = " ".join((str((header or {}).get("supplierName") or ""), str(text or "")))
    if "DHAMECHA" not in supplier_hint.upper() and not looks_like_dhamecha_invoice(supplier_hint):
        return items

    prepared: List[Tuple[Dict[str, Any], str]] = []
    code_rate = {"A": 20.0, "B": 5.0, "R": 5.0, "Z": 0.0}
    header_vat = round(_number((header or {}).get("totalVat")), 2)
    printed_code_vat = 0.0

    for source in items or []:
        if not isinstance(source, dict):
            continue
        row = dict(source)
        existing = str(row.get("vatCode") or row.get("vat_code") or "").strip().upper()
        raw = " ".join(str(row.get(key) or "") for key in ("rawRow", "raw", "description"))
        match = re.search(r"(?:^|\s)([ABZR])\s*$", raw.strip(), flags=re.I)
        if not match:
            match = re.search(r"\d+(?:\.\d+)?%\s*([ABZR])\b", raw, flags=re.I)
        printed = match.group(1).upper() if match else ""
        code = printed if printed in code_rate else (existing if not raw.strip() and existing in code_rate else "")
        if printed in code_rate:
            printed_code_vat += round(_number(row.get("lineNet")) * code_rate[printed] / 100.0, 2)
        prepared.append((row, code))

    printed_codes_cover_header = bool(
        header_vat > 0 and abs(round(printed_code_vat, 2) - header_vat) <= 0.05
    )
    output: List[Dict[str, Any]] = []
    for row, code in prepared:
        if not code and printed_codes_cover_header:
            code = "Z"
        if code in code_rate:
            rate = code_rate[code]
            net = _number(row.get("lineNet"))
            vat = round(net * rate / 100.0, 2) if net and rate > 0 else 0.0
            row.update(
                vatCode=code,
                vatRate=rate,
                vatAmount=vat,
                lineGross=round(net + vat, 2),
            )
        output.append(row)
    return _line_numbers(output)


def repair_header_totals(
    header: Dict[str, Any],
    items: List[Dict[str, Any]],
    raw_text: str = "",
) -> Dict[str, Any]:
    """Prefer a complete, arithmetically valid Dhamecha footer total set."""
    if not header:
        return header
    hint = " ".join((str(header.get("supplierName") or ""), str(raw_text or "")))
    if "DHAMECHA" not in hint.upper() and not looks_like_dhamecha_invoice(hint):
        return header

    def footer(pattern: str) -> float | None:
        match = re.search(pattern, str(raw_text or ""), flags=re.I | re.M)
        return round(_number(match.group(1)), 2) if match else None

    net = footer(r"\bTotal\s+Goods:\s*[^0-9\-]*(-?[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
    vat = footer(r"^\s*VAT:\s*[^0-9\-]*(-?[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)\s*$")
    gross = footer(r"\bTotal\s+incl\s+VAT:\s*[^0-9\-]*(-?[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
    if net is not None and vat is not None and gross is not None and abs(round(net + vat, 2) - gross) <= 0.6:
        header.update(totalNet=net, totalVat=vat, totalGross=gross)
        return header

    current_net = round(_number(header.get("totalNet")), 2)
    current_vat = round(_number(header.get("totalVat")), 2)
    current_gross = round(_number(header.get("totalGross")), 2)
    expected = round(current_net + current_vat, 2)
    if current_net > 0 and current_vat >= 0 and (
        current_gross <= 0 or current_gross < current_net or abs(current_gross - expected) > 0.5
    ):
        header["totalGross"] = expected
    return header


def reduce_duplicate_rows_to_header(
    items: List[Dict[str, Any]],
    header: Dict[str, Any],
    raw_text: str = "",
) -> List[Dict[str, Any]]:
    """Remove exact Dhamecha OCR echoes only when reconciliation improves."""
    if not items or not isinstance(header, dict):
        return items
    hint = " ".join((str(header.get("supplierName") or ""), str(raw_text or "")))
    if "DHAMECHA" not in hint.upper() and not looks_like_dhamecha_invoice(hint):
        return items
    target = round(_number(header.get("totalNet")), 2)
    rows = [dict(item) for item in items if isinstance(item, dict)]
    if target <= 0 or len(rows) < 2:
        return _line_numbers(rows)

    def description_key(value: Any) -> str:
        text = str(value or "").upper()
        text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ML|CL|L|LTR|LITR|LITRE|LITRES|G|KG|OZ|LB)\b", " ", text)
        return " ".join(re.sub(r"[^A-Z0-9]+", " ", text).split())

    def descriptions_match(left: Any, right: Any) -> bool:
        a, b = description_key(left), description_key(right)
        if not a or not b or a == b:
            return True
        wa, wb = set(a.split()), set(b.split())
        return bool(wa and wb and len(wa & wb) >= max(2, min(len(wa), len(wb)) - 1))

    def total(candidate: List[Dict[str, Any]]) -> float:
        return round(sum(_number(row.get("lineNet")) for row in candidate), 2)

    difference = abs(total(rows) - target)
    while difference > 0.05 and len(rows) > 1:
        best_index, best_difference = None, difference
        for left_index, left in enumerate(rows):
            left_code = re.sub(r"\D+", "", str(left.get("code") or ""))
            if not left_code:
                continue
            for right_index in range(left_index + 1, len(rows)):
                right = rows[right_index]
                if left_code != re.sub(r"\D+", "", str(right.get("code") or "")):
                    continue
                if round(_number(left.get("qty")), 3) != round(_number(right.get("qty")), 3):
                    continue
                if round(_number(left.get("unitPrice")), 2) != round(_number(right.get("unitPrice")), 2):
                    continue
                if round(_number(left.get("lineNet")), 2) != round(_number(right.get("lineNet")), 2):
                    continue
                if not descriptions_match(left.get("description"), right.get("description")):
                    continue
                drop = left_index if len(str(left.get("description") or "")) < len(str(right.get("description") or "")) else right_index
                candidate_difference = abs(total(rows[:drop] + rows[drop + 1:]) - target)
                if candidate_difference + 0.01 < best_difference:
                    best_index, best_difference = drop, candidate_difference
        if best_index is None:
            break
        rows.pop(best_index)
        difference = best_difference
    return _line_numbers(rows)

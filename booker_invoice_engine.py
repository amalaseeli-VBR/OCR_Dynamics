from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import extract_purchase_invoice_table as purchase_table


JsonDict = Dict[str, Any]

PRODUCT_CODE_RE = re.compile(r"^\d{5,}\b")
PACK_RE = re.compile(r"^\d+\s+\S+")
QTY_RE = re.compile(r"^-?\d+(?:\.\d+)?-?$")
VAT_CODE_RE = re.compile(r"^[ABZR]$", re.IGNORECASE)
MONEY_RE = re.compile(r"\d+\.\d+")
PERCENT_RE = re.compile(r"\d+\.\d+%")
RRP_POR_RE = re.compile(r"^(\d+\.\d+)\s+(\d+\.\d+%)$")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()


def _bbox(item: Dict[str, Any]) -> Dict[str, float]:
    box = item.get("bbox") if "bbox" in item else item
    return {
        "left": _safe_float(box.get("left")),
        "top": _safe_float(box.get("top")),
        "right": _safe_float(box.get("right")),
        "bottom": _safe_float(box.get("bottom")),
    }


def _mid_x(item: Dict[str, Any]) -> float:
    box = _bbox(item)
    return (box["left"] + box["right"]) / 2.0


def _mid_y(item: Dict[str, Any]) -> float:
    box = _bbox(item)
    return (box["top"] + box["bottom"]) / 2.0


def _extract_money(text: str) -> str:
    match = MONEY_RE.search(_clean_text(text))
    return match.group(0) if match else ""


def _parse_money(text: str) -> Optional[float]:
    token = _extract_money(text)
    if not token:
        return None
    try:
        return float(token)
    except Exception:
        return None


@dataclass
class OCRFragment:
    page: int
    text: str
    left: float
    right: float
    top: float
    bottom: float
    mid_x: float
    mid_y: float

    @classmethod
    def from_line(cls, line: Dict[str, Any]) -> "OCRFragment":
        box = _bbox(line)
        return cls(
            page=int(line.get("page") or 1),
            text=_clean_text(line.get("text")),
            left=box["left"],
            right=box["right"],
            top=box["top"],
            bottom=box["bottom"],
            mid_x=(box["left"] + box["right"]) / 2.0,
            mid_y=(box["top"] + box["bottom"]) / 2.0,
        )


@dataclass
class BookerRow:
    page: int
    row_y: float
    code: str
    description: str
    pack_size: str = ""
    qty: str = ""
    price: str = ""
    value: str = ""
    vat_code: str = ""
    std_rrp: str = ""
    por: str = ""
    fragments: List[OCRFragment] = field(default_factory=list)
    signal_ys: List[float] = field(default_factory=list)
    value_signal_y: Optional[float] = None
    confidence: float = 0.0


@dataclass
class ColumnProfile:
    code_desc_max: float
    pack_qty_mid: float
    qty_price_mid: float
    price_value_mid: float
    value_vat_mid: float
    vat_right_mid: float
    right_edge: float
    header_bottom: float


def _load_payload(path: Path) -> JsonDict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fragments_for_page(payload: JsonDict, page: int) -> List[OCRFragment]:
    lines = payload.get("ocr_lines") or []
    return [OCRFragment.from_line(line) for line in lines if int(line.get("page") or 1) == page]


def _is_noise(text: str) -> bool:
    upper = _clean_text(text).upper()
    blocked_exact = {
        "CODE",
        "DESCRIPTION",
        "PACK SIZE",
        "QTY",
        "PRICE",
        "VALUE",
        "VAT",
        "RRP",
        "POR",
        "VAT RRP POR",
        "VAT RRP",
        "PAYMENT CARD RECEIPT DETAILS",
        "/CONT",
        "RETAIL GROCERY",
        "CHILLED",
        "FROZEN FOOD",
        "CONFECTIONERY",
    }
    if upper in blocked_exact:
        return True
    blocked_contains = (
        "SUB-TOTAL",
        "GOODS",
        "EXC.VAT",
        "EXC. VAT",
        "TOTAL ITEMS",
        "TOTALS:",
        "WINES SPIRITS BEERS",
        "WAITING TO PAY",
        "GRAND TOTAL",
        "INVOICE TOTAL",
    )
    return any(token in upper for token in blocked_contains)


def _build_column_profile(fragments: List[OCRFragment]) -> Optional[ColumnProfile]:
    headers: Dict[str, OCRFragment] = {}
    for frag in fragments:
        text = frag.text.upper()
        if text == "CODE":
            if ("code" not in headers) or (frag.top < headers["code"].top):
                headers["code"] = frag
        elif text == "DESCRIPTION":
            if ("description" not in headers) or (frag.top < headers["description"].top):
                headers["description"] = frag
        elif text == "PACK SIZE":
            if ("pack_size" not in headers) or (frag.top < headers["pack_size"].top):
                headers["pack_size"] = frag
        elif text == "QTY":
            if ("qty" not in headers) or (frag.top < headers["qty"].top):
                headers["qty"] = frag
        elif text == "PRICE":
            if ("price" not in headers) or (frag.top < headers["price"].top):
                headers["price"] = frag
        elif text == "VALUE":
            if ("value" not in headers) or (frag.top < headers["value"].top):
                headers["value"] = frag
        elif text in {"VAT", "VAT RRP POR", "VAT RRP"}:
            if ("vat" not in headers) or (frag.top < headers["vat"].top):
                headers["vat"] = frag
        elif text in {"RRP", "POR"}:
            existing = headers.get("right_edge")
            if (existing is None) or (frag.top < existing.top) or (abs(frag.top - existing.top) < 0.01 and frag.mid_x > existing.mid_x):
                headers["right_edge"] = frag
    required = {"description", "pack_size", "qty", "price", "value"}
    if not required.issubset(headers):
        return None
    header_fragments = [headers[key] for key in headers if key in {"code", "description", "pack_size", "qty", "price", "value", "vat", "right_edge"}]
    header_bottom = max((frag.bottom for frag in header_fragments), default=0.0)
    vat_x = headers["vat"].mid_x if "vat" in headers else min(0.82, headers["value"].mid_x + 0.06)
    right_edge = headers["right_edge"].mid_x if "right_edge" in headers else min(0.94, vat_x + 0.10)
    return ColumnProfile(
        code_desc_max=(headers["description"].mid_x + headers["pack_size"].mid_x) / 2.0,
        pack_qty_mid=(headers["pack_size"].mid_x + headers["qty"].mid_x) / 2.0,
        qty_price_mid=(headers["qty"].mid_x + headers["price"].mid_x) / 2.0,
        price_value_mid=(headers["price"].mid_x + headers["value"].mid_x) / 2.0,
        value_vat_mid=(headers["value"].mid_x + vat_x) / 2.0,
        vat_right_mid=(vat_x + right_edge) / 2.0,
        right_edge=right_edge,
        header_bottom=header_bottom,
    )


def _find_code_rows(fragments: List[OCRFragment], profile: ColumnProfile) -> List[BookerRow]:
    rows: List[BookerRow] = []
    for frag in sorted(fragments, key=lambda item: (item.mid_y, item.left)):
        if frag.mid_x > profile.code_desc_max:
            continue
        if not PRODUCT_CODE_RE.match(frag.text):
            continue
        match = re.match(r"^(\d{5,})\s+(.*)$", frag.text)
        if not match:
            continue
        rows.append(
            BookerRow(
                page=frag.page,
                row_y=frag.mid_y,
                code=match.group(1),
                description=match.group(2).strip(),
                fragments=[frag],
            )
        )
    return rows


def _band_limits(rows: List[BookerRow], profile: ColumnProfile) -> None:
    for idx, row in enumerate(rows):
        prev_y = rows[idx - 1].row_y if idx > 0 else None
        next_y = rows[idx + 1].row_y if idx < len(rows) - 1 else None
        top = (prev_y + row.row_y) / 2.0 if prev_y is not None else max(profile.header_bottom, row.row_y - 0.010)
        bottom = (row.row_y + next_y) / 2.0 if next_y is not None else row.row_y + 0.010
        row._band_top = top  # type: ignore[attr-defined]
        row._band_bottom = bottom  # type: ignore[attr-defined]


def _candidate_rows(frag: OCRFragment, rows: List[BookerRow]) -> List[BookerRow]:
    candidates: List[BookerRow] = []
    for row in rows:
        top = getattr(row, "_band_top", row.row_y - 0.01)
        bottom = getattr(row, "_band_bottom", row.row_y + 0.01)
        if top <= frag.mid_y < bottom:
            candidates.append(row)
    return candidates


def _column_kind(frag: OCRFragment, profile: ColumnProfile) -> str:
    x = frag.mid_x
    if x <= profile.code_desc_max:
        return "left"
    if x <= profile.pack_qty_mid:
        return "pack"
    if x <= profile.qty_price_mid:
        return "qty"
    if x <= profile.price_value_mid:
        return "price"
    if x <= profile.value_vat_mid:
        return "value"
    if x <= profile.vat_right_mid:
        return "vat"
    return "right"


def _score_assignment(row: BookerRow, frag: OCRFragment, profile: ColumnProfile) -> float:
    kind = _column_kind(frag, profile)
    target_y = sum(row.signal_ys) / len(row.signal_ys) if row.signal_ys else row.row_y
    if kind == "right":
        target_y = target_y
    score = 1.0 - min(abs(target_y - frag.mid_y) * 40.0, 1.0)
    text = frag.text
    if kind == "pack" and PACK_RE.match(text):
        score += 0.6
    elif kind == "qty" and QTY_RE.match(text):
        score += 0.6
    elif kind == "price" and MONEY_RE.search(text):
        score += 0.6
    elif kind == "value" and MONEY_RE.search(text):
        score += 0.6
    elif kind == "vat" and (VAT_CODE_RE.match(text) or re.search(r"\b[ABZR]\b", text, re.IGNORECASE)):
        score += 0.6
    elif kind == "right" and (RRP_POR_RE.match(text) or PERCENT_RE.search(text)):
        score += 0.7
    elif kind == "left":
        score -= 1.0
    return score


def _best_row_for_fragment(frag: OCRFragment, rows: List[BookerRow], profile: ColumnProfile) -> Optional[BookerRow]:
    candidates = _candidate_rows(frag, rows)
    if not candidates:
        return None
    ranked = sorted(candidates, key=lambda row: _score_assignment(row, frag, profile), reverse=True)
    best = ranked[0]
    if _score_assignment(best, frag, profile) < 0.35:
        return None
    return best


def _nearest_row_by_y(
    rows: List[BookerRow],
    target_y: float,
    threshold: float,
    use_signal_center: bool = False,
    prefer_value_signal: bool = False,
) -> Optional[BookerRow]:
    best: Optional[BookerRow] = None
    best_dist = 999.0
    for row in rows:
        if prefer_value_signal and row.value_signal_y is not None:
            center = row.value_signal_y
        elif use_signal_center and row.signal_ys:
            center = sum(row.signal_ys) / len(row.signal_ys)
        else:
            center = row.row_y
        dist = abs(center - target_y)
        if dist < best_dist:
            best = row
            best_dist = dist
    if best is None or best_dist > threshold:
        return None
    return best


def _append_fragment(row: BookerRow, frag: OCRFragment, profile: ColumnProfile) -> None:
    kind = _column_kind(frag, profile)
    text = frag.text
    row.fragments.append(frag)
    if kind == "pack" and PACK_RE.match(text) and not row.pack_size:
        row.pack_size = text
        row.signal_ys.append(frag.mid_y)
    elif kind == "qty" and QTY_RE.match(text) and not row.qty:
        row.qty = text
        row.signal_ys.append(frag.mid_y)
    elif kind == "price" and not row.price:
        row.price = text
        row.signal_ys.append(frag.mid_y)
    elif kind == "value":
        row.value = _clean_text(" ".join(part for part in [row.value, text] if part))
        row.signal_ys.append(frag.mid_y)
        row.value_signal_y = frag.mid_y
    elif kind == "vat":
        if VAT_CODE_RE.match(text) and not row.vat_code:
            row.vat_code = text.upper()
            row.signal_ys.append(frag.mid_y)
        else:
            row.value = _clean_text(" ".join(part for part in [row.value, text] if part))
            row.signal_ys.append(frag.mid_y)
            row.value_signal_y = frag.mid_y
    elif kind == "right":
        mixed_value_vat = re.match(r"^(\d+\.\d+)\s*([ABZR])$", text, re.IGNORECASE)
        if mixed_value_vat:
            if not row.value:
                row.value = mixed_value_vat.group(1)
                row.value_signal_y = frag.mid_y
            if not row.vat_code:
                row.vat_code = mixed_value_vat.group(2).upper()
            row.signal_ys.append(frag.mid_y)
            return
        match = RRP_POR_RE.match(text)
        if match and not (row.std_rrp and row.por):
            row.std_rrp = row.std_rrp or match.group(1)
            row.por = row.por or match.group(2)
            return
        if VAT_CODE_RE.match(text) and not row.vat_code:
            row.vat_code = text.upper()
            row.signal_ys.append(frag.mid_y)
            return
        if PERCENT_RE.fullmatch(text) and not row.por:
            row.por = text
            return
        if MONEY_RE.fullmatch(text):
            if not row.std_rrp:
                row.std_rrp = text
            elif not row.value:
                row.value = text
                row.value_signal_y = frag.mid_y
                row.signal_ys.append(frag.mid_y)


def _normalize_row(row: BookerRow) -> None:
    if row.value:
        vat_match = re.search(r"\b([ABZR])\b", row.value, re.IGNORECASE)
        if vat_match and not row.vat_code:
            row.vat_code = vat_match.group(1).upper()
        money_tokens = MONEY_RE.findall(row.value)
        if money_tokens:
            row.value = money_tokens[0]
    if not row.std_rrp or not row.por:
        for frag in sorted(row.fragments, key=lambda item: item.mid_x):
            match = RRP_POR_RE.match(frag.text)
            if match:
                row.std_rrp = row.std_rrp or match.group(1)
                row.por = row.por or match.group(2)
            elif MONEY_RE.fullmatch(frag.text) and frag.mid_x > 0.84:
                row.std_rrp = row.std_rrp or frag.text
            elif PERCENT_RE.fullmatch(frag.text) and frag.mid_x > 0.84:
                row.por = row.por or frag.text
            elif re.match(r"^(\d+\.\d+)\s*([ABZR])$", frag.text, re.IGNORECASE):
                mixed = re.match(r"^(\d+\.\d+)\s*([ABZR])$", frag.text, re.IGNORECASE)
                if mixed:
                    row.value = row.value or mixed.group(1)
                    row.vat_code = row.vat_code or mixed.group(2).upper()
    if not row.price and row.value and row.qty:
        value_num = _parse_money(row.value)
        qty_num = _parse_money(row.qty)
        if value_num is not None and qty_num not in (None, 0):
            row.price = f"{value_num / qty_num:.2f}"
    if not row.value and row.price and row.qty:
        price_num = _parse_money(row.price)
        qty_num = _parse_money(row.qty)
        if price_num is not None and qty_num not in (None, 0):
            row.value = f"{price_num * qty_num:.2f}"


def _row_confidence(row: BookerRow) -> float:
    score = 0.30
    for present, weight in (
        (bool(row.code), 0.20),
        (bool(row.description), 0.15),
        (bool(row.pack_size), 0.07),
        (bool(row.qty), 0.07),
        (bool(row.price), 0.07),
        (bool(row.value), 0.07),
        (bool(row.vat_code), 0.04),
        (bool(row.std_rrp), 0.01),
        (bool(row.por), 0.02),
    ):
        if present:
            score += weight
    row.confidence = round(min(score, 0.99), 2)
    return row.confidence


def parse_booker_purchase(payload: JsonDict) -> List[JsonDict]:
    pages = sorted({int(line.get("page") or 1) for line in (payload.get("ocr_lines") or [])})
    raw_rows: List[JsonDict] = []
    for page in pages:
        fragments = _fragments_for_page(payload, page)
        profile = _build_column_profile(fragments)
        if not profile:
            continue
        rows = _find_code_rows(fragments, profile)
        if not rows:
            continue
        _band_limits(rows, profile)
        non_left = [
            frag
            for frag in sorted(fragments, key=lambda item: (item.mid_y, item.left))
            if frag.text and not _is_noise(frag.text) and not PRODUCT_CODE_RE.match(frag.text) and frag.mid_x > profile.code_desc_max
        ]

        stage1_kinds = {"pack", "qty", "price"}
        for frag in non_left:
            kind = _column_kind(frag, profile)
            if kind not in stage1_kinds:
                continue
            row = _nearest_row_by_y(rows, frag.mid_y, threshold=0.012, use_signal_center=False)
            if row is None:
                continue
            _append_fragment(row, frag, profile)

        for row in rows:
            if not row.signal_ys:
                row.signal_ys.append(row.row_y)

        for frag in non_left:
            kind = _column_kind(frag, profile)
            if kind in stage1_kinds:
                continue
            threshold = 0.011 if kind in {"value", "vat", "right"} else 0.010
            row = _nearest_row_by_y(
                rows,
                frag.mid_y,
                threshold=threshold,
                use_signal_center=True,
                prefer_value_signal=(kind == "right"),
            )
            if row is None:
                continue
            _append_fragment(row, frag, profile)

        for idx, row in enumerate(rows, start=len(raw_rows) + 1):
            _normalize_row(row)
            _row_confidence(row)
            raw_rows.append(
                {
                    "line_no": idx,
                    "page": row.page,
                    "row_y": round(row.row_y, 6),
                    "code": row.code,
                    "description": row.description,
                    "pack_size": row.pack_size,
                    "qty": row.qty,
                    "price": row.price,
                    "value": row.value,
                    "vat": row.vat_code,
                    "rrp_por": _clean_text(" ".join(part for part in [row.std_rrp, row.por] if part)),
                    "_confidence": row.confidence,
                }
            )
    return raw_rows


def _normalize_rows(rows: List[JsonDict], payload: JsonDict) -> List[JsonDict]:
    vat_rates = purchase_table._extract_vat_rate_map(payload)
    enriched = purchase_table._enrich_common_columns(rows, vat_rates)
    renamed = purchase_table._rename_output_columns(enriched)
    for row, raw in zip(renamed, rows):
        row["_confidence"] = raw.get("_confidence", 0.0)
    return renamed


def _header_data(payload: JsonDict) -> JsonDict:
    hay = "\n".join([str(payload.get("raw_text") or ""), str(payload.get("line_text") or "")])
    invoice_number = ""
    invoice_date = ""
    total = None
    number_match = re.search(r"INVOICE NUMBER\s*[:\-]?\s*([A-Z0-9\-\/]+)", hay, re.IGNORECASE)
    if number_match:
        invoice_number = _clean_text(number_match.group(1))
    date_match = re.search(r"\bDATE\s*[:\-]?\s*(\d{2}/\d{2}/\d{2,4})", hay, re.IGNORECASE)
    if date_match:
        invoice_date = _clean_text(date_match.group(1))
    for pattern in [r"INVOICE TOTAL\s*[:\-]?\s*([0-9]+\.[0-9]{2})", r"GRAND TOTAL\s*[:\-]?\s*([0-9]+\.[0-9]{2})"]:
        match = re.search(pattern, hay, re.IGNORECASE)
        if match:
            total = _parse_money(match.group(1))
            break
    return {
        "supplier": purchase_table.detect_supplier_family(payload),
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "invoice_total": total,
    }


def build_response(payload: JsonDict) -> JsonDict:
    raw_rows = parse_booker_purchase(payload)
    rows = _normalize_rows(raw_rows, payload)
    header = _header_data(payload)
    line_total = sum(_parse_money(str(row.get("Total/Net") or "")) or 0.0 for row in rows)
    warnings: List[str] = []
    if header.get("invoice_total") is not None:
        delta = abs(header["invoice_total"] - line_total)
        if delta > 0.5:
            warnings.append(f"Line total {line_total:.2f} does not reconcile to invoice total {header['invoice_total']:.2f}")
    line_items = []
    for idx, row in enumerate(rows, start=1):
        line_items.append(
            {
                "line_no": int(row.get("line_no") or idx),
                "page": row.get("page"),
                "code": _clean_text(row.get("code")),
                "description": _clean_text(row.get("description")),
                "pmp": _clean_text(row.get("pmp")),
                "case_pack": _clean_text(row.get("Case Pack")),
                "unit_size": _clean_text(row.get("Unit Size")),
                "quantity": _clean_text(row.get("Quantity")),
                "total_units": _clean_text(row.get("Total units")),
                "unit_price": _clean_text(row.get("Unit Price")),
                "total_net": _clean_text(row.get("Total/Net")),
                "vat_code": _clean_text(row.get("VAT code")),
                "vat_percent": _clean_text(row.get("VAT %")),
                "vat_amount": _clean_text(row.get("VAT Amount")),
                "gross": _clean_text(row.get("Gross")),
                "std_rrp": _clean_text(row.get("Std RRP")),
                "por": _clean_text(row.get("por")),
                "confidence": row.get("_confidence", 0.0),
            }
        )
    return {
        "source_file": payload.get("source_file"),
        "supplier": header["supplier"],
        "invoice_number": header["invoice_number"],
        "invoice_date": header["invoice_date"],
        "invoice_total": header["invoice_total"],
        "strategy": "booker_engine_v1",
        "warnings": warnings,
        "meta": {
            "line_item_count": len(line_items),
            "line_net_total": round(line_total, 2),
        },
        "line_items": line_items,
    }


def _write_json(path: Path, data: JsonDict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_csv(path: Path, rows: Iterable[JsonDict]) -> None:
    fields = [
        "line_no",
        "page",
        "code",
        "description",
        "pmp",
        "case_pack",
        "unit_size",
        "quantity",
        "total_units",
        "unit_price",
        "total_net",
        "vat_code",
        "vat_percent",
        "vat_amount",
        "gross",
        "std_rrp",
        "por",
        "confidence",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone Booker invoice parser engine.")
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--csv-out", type=Path)
    args = parser.parse_args()

    payload = _load_payload(args.json_path)
    response = build_response(payload)
    if args.json_out:
        _write_json(args.json_out, response)
    else:
        print(json.dumps(response, indent=2, ensure_ascii=False))
    if args.csv_out:
        _write_csv(args.csv_out, response["line_items"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

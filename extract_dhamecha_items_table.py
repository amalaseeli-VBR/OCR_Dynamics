from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _load_payload(json_path: Path) -> Dict[str, Any]:
    return json.loads(json_path.read_text(encoding="utf-8"))


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()


def _ascii_safe(value: str) -> str:
    return str(value).encode("ascii", "replace").decode("ascii")


def _is_header_line(text: str) -> bool:
    upper = _clean_text(text).upper()
    return (
        upper == "ITEM #"
        or "PRODUCT DESCRIPTION" in upper
        or "PRICE(" in upper
        or upper == "VAT"
    )


def _is_footer_line(text: str) -> bool:
    upper = _clean_text(text).upper()
    blocked = (
        "TOTAL",
        "SUBTOTAL",
        "SUB TOTAL",
        "VAT SUMMARY",
        "NET AMOUNT",
        "GROSS AMOUNT",
        "CARD PAYMENT",
        "CASH PAYMENT",
        "CHANGE",
        "THANK YOU",
    )
    return any(token in upper for token in blocked)


def _looks_like_code_and_desc(text: str) -> Optional[Tuple[str, str]]:
    cleaned = _clean_text(text)
    match = re.match(r"^(\d{3,8})\s+(.*)$", cleaned)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def _looks_like_code_only(text: str) -> Optional[str]:
    cleaned = _clean_text(text)
    if re.fullmatch(r"\d{3,8}", cleaned):
        return cleaned
    return None


def _looks_like_unit_size(text: str) -> bool:
    value = _clean_text(text)
    return bool(re.fullmatch(r"\d+(?:\.\d+)?\s*(ML|L|LTR|LTRS|LT|CL|G|KG)", value, flags=re.IGNORECASE))


def _looks_like_integer(text: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", _clean_text(text)))


def _looks_like_money(text: str) -> bool:
    value = _clean_text(text).replace(",", "")
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?P?", value, flags=re.IGNORECASE))


def _looks_like_percent(text: str) -> bool:
    value = _clean_text(text).replace(" ", "")
    return bool(re.fullmatch(r"-?\d+(?:\.\d+)?%?", value))


def _money_value(text: str) -> float:
    value = _clean_text(text).replace(",", "")
    value = value.rstrip("Pp")
    return _safe_float(value)


def _percent_value(text: str) -> float:
    value = _clean_text(text).replace("%", "")
    return _safe_float(value)


def _load_lines(payload: Dict[str, Any]) -> List[Tuple[int, str]]:
    rows: List[Tuple[int, str]] = []
    for line in list(payload.get("ocr_lines") or []):
        text = _clean_text(line.get("text"))
        if not text:
            continue
        rows.append((int(line.get("page") or 1), text))
    return rows


def extract_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = _load_lines(payload)
    items: List[Dict[str, Any]] = []
    collecting = False
    i = 0

    while i < len(lines):
        page, text = lines[i]

        if _is_header_line(text):
            if "PRODUCT DESCRIPTION" in text.upper():
                collecting = True
            i += 1
            continue

        if not collecting:
            i += 1
            continue

        if _is_footer_line(text):
            break

        code_desc = _looks_like_code_and_desc(text)
        code_only = _looks_like_code_only(text)
        if not code_desc and not code_only:
            i += 1
            continue

        if code_desc:
            code, description = code_desc
        else:
            code = str(code_only or "")
            description = ""
        block: List[str] = []
        j = i + 1
        while j < len(lines):
            next_page, next_text = lines[j]
            if next_page != page:
                break
            if _looks_like_code_and_desc(next_text) or _looks_like_code_only(next_text):
                break
            if _is_footer_line(next_text):
                break
            if _is_header_line(next_text):
                j += 1
                continue
            block.append(next_text)
            j += 1

        unit_size = ""
        case_pack = 0
        qty = 0.0
        unit_price = 0.0
        line_net = 0.0
        rrp = 0.0
        por = 0.0
        vat_code = ""

        description_parts: List[str] = []
        numeric_tail: List[str] = []
        for entry in block:
            cleaned_entry = _clean_text(entry)
            if not cleaned_entry:
                continue
            if not unit_size and _looks_like_unit_size(entry):
                unit_size = entry
                continue
            if case_pack == 0 and _looks_like_integer(entry):
                candidate = int(cleaned_entry)
                if candidate > 1:
                    case_pack = candidate
                    continue
            if not _looks_like_integer(entry) and not _looks_like_money(entry) and not _looks_like_percent(entry):
                if cleaned_entry.upper() not in {"A", "B", "C", "Z", "R"}:
                    description_parts.append(cleaned_entry)
                    continue
            numeric_tail.append(entry)

        if description_parts:
            description = " ".join(part for part in [description] + description_parts if part).strip()

        filtered = [entry for entry in numeric_tail if _clean_text(entry)]
        if len(filtered) >= 6:
            qty = _safe_float(_clean_text(filtered[0]))
            unit_price = _money_value(filtered[1])
            line_net = _money_value(filtered[2])
            rrp = _money_value(filtered[3]) if _looks_like_money(filtered[3]) else 0.0
            por = _percent_value(filtered[4]) if _looks_like_percent(filtered[4]) else 0.0
            vat_code = _clean_text(filtered[5]).upper()
        else:
            numbers = [_clean_text(entry) for entry in filtered]
            if len(numbers) >= 1:
                qty = _safe_float(numbers[0])
            if len(numbers) >= 2:
                unit_price = _money_value(numbers[1])
            if len(numbers) >= 3:
                line_net = _money_value(numbers[2])
            if len(numbers) >= 4 and _looks_like_money(numbers[3]):
                rrp = _money_value(numbers[3])
            if len(numbers) >= 5 and _looks_like_percent(numbers[4]):
                por = _percent_value(numbers[4])
            if numbers:
                tail = numbers[-1].upper()
                if tail in {"A", "B", "C", "Z", "R"}:
                    vat_code = tail

        vat_rate_map = {"A": 0.0, "B": 20.0, "C": 5.0, "Z": 0.0, "R": 5.0}
        vat_rate = float(vat_rate_map.get(vat_code, 0.0))
        vat_amount = round(line_net * (vat_rate / 100.0), 2) if line_net and vat_rate else 0.0
        line_gross = round(line_net + vat_amount, 2) if line_net else 0.0
        total_units = round(qty * case_pack, 2) if qty and case_pack else 0.0

        items.append(
            {
                "line_no": len(items) + 1,
                "page": page,
                "code": code,
                "description": description,
                "unit_size": unit_size,
                "case_pack": case_pack,
                "qty": qty,
                "unit_price": unit_price,
                "line_net": line_net,
                "vat_code": vat_code,
                "vat_rate": vat_rate,
                "vat_amount": vat_amount,
                "line_gross": line_gross,
                "rrp": rrp,
                "por": por,
                "total_units": total_units,
            }
        )

        i = j

    return items


def _print_rows(rows: List[Dict[str, Any]]) -> None:
    headers = [
        "line_no",
        "page",
        "code",
        "description",
        "unit_size",
        "case_pack",
        "qty",
        "unit_price",
        "line_net",
        "vat_code",
        "vat_rate",
        "vat_amount",
        "line_gross",
        "rrp",
        "por",
        "total_units",
    ]
    widths = {header: len(header) for header in headers}
    display: List[Dict[str, str]] = []

    for row in rows:
        rendered = {}
        for header in headers:
            value = row.get(header, "")
            if isinstance(value, float):
                rendered[header] = f"{value:.2f}"
            else:
                rendered[header] = str(value)
            rendered[header] = _ascii_safe(rendered[header])
            widths[header] = max(widths[header], min(len(rendered[header]), 40))
        display.append(rendered)

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
    for row in display:
        print(fmt(row))


def _write_csv(rows: List[Dict[str, Any]], csv_path: Path) -> None:
    fieldnames = [
        "line_no",
        "page",
        "code",
        "description",
        "unit_size",
        "case_pack",
        "qty",
        "unit_price",
        "line_net",
        "vat_code",
        "vat_rate",
        "vat_amount",
        "line_gross",
        "rrp",
        "por",
        "total_units",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract Dhamecha line items from Document AI OCR JSON.")
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

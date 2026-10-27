from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List


def looks_like_tns_invoice(text: str) -> bool:
    up = str(text or "").upper()
    return (
        ("TNS WHOLESALE" in up or "TNS RETAIL LIMITED" in up)
        and "INVOICE" in up
        and ("VAT SUMMARY" in up or "TOTAL NO ITEMS" in up)
    )


def _money(value: Any) -> float:
    raw = str(value or "").replace(",", "")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    try:
        return float(raw) if raw not in {"", "-", "."} else 0.0
    except Exception:
        return 0.0


def _date(value: str) -> str:
    value = str(value or "").strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except Exception:
            pass
    return ""


def _label_next(lines: List[str], label: str) -> str:
    target = label.upper()
    for index, line in enumerate(lines):
        if line.upper() == target and index + 1 < len(lines):
            return lines[index + 1]
    return ""


def _labelled_date(text: str, label: str) -> str:
    """Read a TNS date whether OCR puts the value beside or below its label."""
    match = re.search(
        rf"\b{re.escape(label)}\b\s*[:\-]?\s*"
        r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        str(text or ""),
        re.I,
    )
    if match:
        return _date(match.group(1))
    # Column-preserving PDF text can put the label at the end of one row and
    # the value at the end of the next row, after unrelated left-column text.
    source_lines = str(text or "").splitlines()
    for index, line in enumerate(source_lines):
        if label.upper() not in line.upper():
            continue
        nearby = " ".join(source_lines[index:index + 3])
        nearby_match = re.search(
            r"([A-Za-z]{3,9}\s+\d{1,2},\s+\d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{4})",
            nearby,
            re.I,
        )
        if nearby_match:
            return _date(nearby_match.group(1))
    return ""


def _tns_customer(lines: List[str], joined: str) -> str:
    """Extract BILL TO customer from stacked or side-by-side OCR columns."""
    bill_start = next((i for i, line in enumerate(lines) if line.upper() == "BILL TO:"), -1)
    ship_start = next((i for i, line in enumerate(lines) if line.upper() == "SHIP TO:"), len(lines))
    if bill_start >= 0 and ship_start > bill_start:
        bill_lines = lines[bill_start + 1:ship_start]
        for wanted in ("COMPANY:", "NAME:"):
            for line in bill_lines:
                if line.upper().startswith(wanted):
                    return line.split(":", 1)[1].strip()

    # DocTR frequently rowifies the two address panels as:
    # "BILL TO: SHIP TO:" then "Company: <bill> Company: <ship>".
    bill_text = joined
    bill_marker = re.search(r"\bBILL\s+TO\s*:", bill_text, re.I)
    if bill_marker:
        bill_text = bill_text[bill_marker.end():]
    total_marker = re.search(r"\bDescription\b", bill_text, re.I)
    if total_marker:
        bill_text = bill_text[:total_marker.start()]
    for field in ("Company", "Name"):
        match = re.search(
            rf"\b{field}\s*:\s*(.+?)"
            rf"(?=\s+(?:Company|Name|Email|Phone|Address)\s*:|\n|$)",
            bill_text,
            re.I,
        )
        if match:
            return match.group(1).strip(" |-:")
    return ""


def parse_tns_invoice(text: str) -> Dict[str, Any]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    joined = "\n".join(lines)

    invoice_number = ""
    match = re.search(r"\bInv(?:oice)?\s*No\s*:\s*([A-Z0-9][A-Z0-9-]{3,})", joined, re.I)
    if match:
        invoice_number = match.group(1)

    customer = _tns_customer(lines, joined)

    header: Dict[str, Any] = {
        "supplierName": "TNS Retail Limited",
        "customerName": customer,
        "invoiceNumber": invoice_number,
        "invoiceDate": (
            _labelled_date(joined, "Invoice Issue Date")
            or _date(_label_next(lines, "Invoice Issue Date"))
        ),
        "orderNumber": _label_next(lines, "Order No"),
        "currency": "GBP",
        "totalNet": 0.0,
        "totalVat": 0.0,
        "totalGross": 0.0,
    }

    summary = joined[joined.upper().find("VAT SUMMARY"):] if "VAT SUMMARY" in joined.upper() else joined
    matches = re.findall(r"Subtotal(?:\s*\([^)]*\))?\s*:\s*[^0-9\-]*([0-9,.]+)", summary, re.I)
    if not matches:
        matches = re.findall(r"Subtotal(?:\s*\([^)]*\))?\s*:\s*[^0-9\-]*([0-9,.]+)", joined, re.I)
    if matches:
        header["totalNet"] = _money(matches[-1])
    matches = re.findall(r"(?m)^VAT(?:\s*\([^)]*\))?\s*:\s*[^0-9\-]*([0-9,.]+)", summary, re.I)
    if not matches:
        # Layout text may place the VAT total after the VAT-summary rate row on
        # the same physical line. Exclude the separate Shipping VAT field.
        matches = re.findall(
            r"(?<!Shipping\s)\bVAT(?:\s*\([^)]*\))?\s*:\s*[^0-9\-]*([0-9,.]+)",
            joined,
            re.I,
        )
    if matches:
        header["totalVat"] = _money(matches[-1])
    matches = re.findall(r"(?m)^Total:\s*[^0-9\-]*([0-9,.]+)", summary, re.I)
    if not matches:
        matches = re.findall(r"\bTotal:\s*[^0-9\-]*([0-9,.]+)", joined, re.I)
    if matches:
        header["totalGross"] = _money(matches[-1])

    try:
        table_start = next(
            i for i, line in enumerate(lines)
            if line.upper() == "POR%"
            or ("DESCRIPTION" in line.upper() and "QTY" in line.upper() and "POR%" in line.upper())
        ) + 1
    except StopIteration:
        table_start = 0
    try:
        table_end = next(i for i in range(table_start, len(lines)) if lines[i].upper().startswith("TOTAL NO ITEMS"))
    except StopIteration:
        table_end = len(lines)

    code_re = re.compile(r"^(?=[A-Z0-9_\-]*\d)[A-Z0-9_\-]{5,}$", re.I)
    number_re = re.compile(r"^\d+(?:\.\d+)?$")
    money_re = re.compile(r"^[^0-9\-]*\d[0-9,.]*$")
    percent_re = re.compile(r"^(?:-|\d+(?:\.\d+)?%)$")
    def _valid_tail(values: List[str]) -> bool:
        return (
            len(values) == 7
            and bool(number_re.fullmatch(values[0]))
            and (values[1] == "-" or bool(number_re.fullmatch(values[1])))
            and bool(money_re.fullmatch(values[2]))
            and bool(money_re.fullmatch(values[3]))
            and bool(percent_re.fullmatch(values[4]))
            and (values[5] == "-" or bool(money_re.fullmatch(values[5])))
            and bool(percent_re.fullmatch(values[6]))
        )

    def _tail_in_block(block: List[str]) -> tuple[List[str], set[int]]:
        """Find Qty/Pack/Price/Net/VAT/RRP/POR in either PDF text layout.

        Poppler/PyPDF commonly returns all seven cells on one line, whereas
        DocTR often emits one cell per line.  TNS also moves the description
        before or after those cells depending on the extraction mode.
        """
        for index, line in enumerate(block):
            values = line.split()
            if _valid_tail(values):
                return values, {index}
        for index in range(max(0, len(block) - 6)):
            values = block[index:index + 7]
            if _valid_tail(values):
                return values, set(range(index, index + 7))
        return [], set()

    repeated = {"DESCRIPTION", "QTY", "PACK", "PRICE", "NET", "VAT%", "RRP", "POR%"}
    items: List[Dict[str, Any]] = []
    table_lines = lines[table_start:table_end]

    # In column-preserving text, a description and its seven numeric cells can
    # occupy the same physical line. Split a valid seven-token suffix into the
    # same logical shape produced by raw PDF text.
    split_table_lines: List[str] = []
    for line in table_lines:
        tokens = line.split()
        tail_suffix = tokens[-7:] if len(tokens) > 7 else []
        if tail_suffix and _valid_tail(tail_suffix):
            leading = " ".join(tokens[:-7]).strip()
            if leading:
                split_table_lines.append(leading)
            split_table_lines.append(" ".join(tail_suffix))
        else:
            split_table_lines.append(line)
    table_lines = split_table_lines

    # Locate every numeric row tail independently of product code. Some TNS
    # invoices intentionally leave the code column blank on individual rows.
    tail_spans: List[tuple[int, int, List[str]]] = []
    cursor = 0
    while cursor < len(table_lines):
        inline_values = table_lines[cursor].split()
        if _valid_tail(inline_values):
            tail_spans.append((cursor, cursor + 1, inline_values))
            cursor += 1
            continue
        stacked_values = table_lines[cursor:cursor + 7]
        if _valid_tail(stacked_values):
            tail_spans.append((cursor, cursor + 7, stacked_values))
            cursor += 7
            continue
        cursor += 1

    # Native/raw PDF text puts description before the numeric tail. Layout OCR
    # often puts it after. Infer the dominant direction from the first coded row.
    description_before_tail = True
    if tail_spans:
        first_start = tail_spans[0][0]
        first_code = next(
            (i for i in range(first_start - 1, -1, -1) if code_re.fullmatch(table_lines[i])),
            -1,
        )
        if first_code >= 0:
            between = table_lines[first_code + 1:first_start]
            description_before_tail = any(line.upper() not in repeated for line in between)

    def _clean_description(source: List[str]) -> List[str]:
        return [
            line for line in source
            if line.upper() not in repeated
            and not re.fullmatch(r"Page\s+\d+", line, re.I)
            and not re.match(r"Invoice\s+No\s*:", line, re.I)
            and not code_re.fullmatch(line)
        ]

    previous_end = 0
    for tail_index, (tail_start, tail_end, tail) in enumerate(tail_spans):
        next_start = tail_spans[tail_index + 1][0] if tail_index + 1 < len(tail_spans) else len(table_lines)
        prefix = table_lines[previous_end:tail_start]
        previous_end = tail_end
        code_positions = [i for i, line in enumerate(prefix) if code_re.fullmatch(line)]
        code = prefix[code_positions[-1]] if code_positions else ""

        if description_before_tail:
            description_source = prefix[code_positions[-1] + 1:] if code_positions else prefix
        else:
            suffix = table_lines[tail_end:next_start]
            next_code = next((i for i, line in enumerate(suffix) if code_re.fullmatch(line)), len(suffix))
            description_source = suffix[:next_code]

        description_lines = _clean_description(description_source)
        description = " ".join(description_lines).strip()
        if not description:
            # Tolerate a single page whose OCR order differs from the dominant
            # document order.
            suffix = table_lines[tail_end:next_start]
            next_code = next((i for i, line in enumerate(suffix) if code_re.fullmatch(line)), len(suffix))
            description = " ".join(_clean_description(suffix[:next_code])).strip()
        if not description:
            continue
        qty_s, pack_s, price_s, net_s, vat_s, rrp_s, por_s = tail
        qty = float(qty_s)
        case_pack = 0
        pack_match = re.search(r"(\d+)\s*PACKS?\b", description, re.I)
        if re.fullmatch(r"\d+", pack_s):
            case_pack = int(pack_s)
        elif pack_match:
            case_pack = int(pack_match.group(1))
        vat_rate = _money(vat_s)
        line_net = _money(net_s)
        vat_amount = round(line_net * vat_rate / 100.0, 2)
        items.append({
            "lineNo": len(items) + 1,
            "code": code,
            "barcode": "",
            "description": description,
            "casePack": case_pack,
            "casePackText": pack_s if pack_s != "-" else (str(case_pack) if case_pack else ""),
            "unitSize": "",
            "qty": qty,
            "totalUnits": qty * case_pack if case_pack else 0.0,
            "unitPrice": _money(price_s),
            "lineNet": line_net,
            "vatCode": str(int(vat_rate)) if vat_rate else "0",
            "vatRate": vat_rate,
            "vatAmount": vat_amount,
            "lineGross": round(line_net + vat_amount, 2),
            "stdRrp": _money(rrp_s),
            "por": _money(por_s),
            "raw": " | ".join(([code] if code else []) + description_lines + tail),
            "rawRow": " | ".join(([code] if code else []) + description_lines + tail),
        })

    return {"header": header, "items": items}

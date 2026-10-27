from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Tuple


BESTWAY_VAT_RATES = {"1": 20.0, "26": 0.0}


def bestway_text_from_ocr_tokens(tokens: List[Dict[str, Any]]) -> str:
    """Rebuild Bestway product rows from positioned OCR word tokens.

    DocTR commonly returns a Bestway row as several separate structural lines:
    code/description, pack, quantity/price, line value/VAT, and SRP/POR.  The
    barcode/description baseline is the most stable row anchor, so assign nearby
    tokens to the closest barcode on the same page and restore x order. The
    left-hand six-digit code is used only when barcode OCR is unavailable.
    """
    by_page: Dict[int, List[Tuple[float, float, float, float, str]]] = {}
    for token in tokens or []:
        if not isinstance(token, dict):
            continue
        text = str(token.get("text") or "").strip()
        bbox = token.get("bbox") or []
        if not text or not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            continue
        try:
            x0, y0, x1, y1 = (float(bbox[i]) for i in range(4))
            page = int(token.get("page") or 1)
        except Exception:
            continue
        by_page.setdefault(page, []).append((x0, y0, x1, y1, text))

    rebuilt: List[str] = []
    for page in sorted(by_page):
        page_tokens = by_page[page]
        if not page_tokens:
            continue
        page_width = max((tok[2] for tok in page_tokens), default=1.0) or 1.0
        anchors: List[Tuple[float, int]] = []
        for index, (x0, y0, x1, y1, text) in enumerate(page_tokens):
            # EAN-style barcodes sit immediately before the description and
            # share its baseline. This is more reliable than product-code y,
            # which DocTR sometimes places above or below the rest of the row.
            if page_width * 0.07 <= x0 <= page_width * 0.42 and re.fullmatch(r"\d{11,14}", text):
                anchors.append(((y0 + y1) / 2.0, index))
        if not anchors:
            for index, (x0, y0, x1, y1, text) in enumerate(page_tokens):
                if x0 <= page_width * 0.075 and re.fullmatch(r"\d{6}", text):
                    anchors.append(((y0 + y1) / 2.0, index))
        anchors.sort(key=lambda pair: pair[0])
        if not anchors:
            continue

        centers = [pair[0] for pair in anchors]
        for anchor_pos, (anchor_y, anchor_index) in enumerate(anchors):
            upper = -float("inf") if anchor_pos == 0 else (centers[anchor_pos - 1] + anchor_y) / 2.0
            lower = float("inf") if anchor_pos == len(anchors) - 1 else (anchor_y + centers[anchor_pos + 1]) / 2.0
            row_tokens = []
            for tok in page_tokens:
                cy = (tok[1] + tok[3]) / 2.0
                if upper < cy <= lower:
                    row_tokens.append(tok)
            row_tokens.sort(key=lambda tok: (tok[0], (tok[1] + tok[3]) / 2.0))
            row_text = " ".join(tok[4] for tok in row_tokens).strip()
            # Keep only genuine product-shaped rows; the deterministic parser
            # performs the stricter barcode/pack/value validation afterwards.
            anchor_text = page_tokens[anchor_index][4]
            if row_text and anchor_text in row_text:
                rebuilt.append(row_text)
    return "\n".join(rebuilt)


def looks_like_bestway_invoice(text: str) -> bool:
    """Return True for Bestway Wholesale till/collection invoices."""
    up = str(text or "").upper()
    return bool(
        ("BESTWAY" in up and "WHOLESALE" in up and "INVOICE" in up)
        or (
            "MERGE TRANSACTION" in up
            and "TROLLEY COUNT" in up
            and "LINE VALUE" in up
            and "VAT" in up
        )
    )


def _clean_text(text: str) -> str:
    value = str(text or "")
    # Common UTF-8/Windows OCR renderings of the pound sign.
    for token in ("Â£", "Â£", "â‚¬", "€"):
        value = value.replace(token, "£")
    value = value.replace("×", "x")
    return value


def _money(token: Any) -> float:
    raw = str(token or "").strip()
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    elif "," in raw:
        comma_tail = raw.rsplit(",", 1)[-1]
        raw = raw.replace(",", "." if len(comma_tail) <= 2 else "")
    raw = re.sub(r"[^0-9.\-]", "", raw)
    if not raw or raw in {".", "-", "-."}:
        return 0.0
    try:
        # OCR regularly drops the decimal point after a visible pound sign.
        if "." not in raw and raw.lstrip("-").isdigit() and len(raw.lstrip("-")) >= 3:
            sign = -1.0 if raw.startswith("-") else 1.0
            return sign * (int(raw.lstrip("-")) / 100.0)
        return float(raw)
    except Exception:
        return 0.0


def _iso_date(value: str) -> str:
    value = str(value or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except Exception:
            pass
    return ""


def _extract_header(text: str) -> Dict[str, Any]:
    header: Dict[str, Any] = {
        "supplierName": "Bestway Wholesale",
        "customerName": "",
        "companyName": "N/A",
        "tradingName": "",
        "customerNo": "",
        "customerPONumber": "",
        "invoiceNumber": "",
        "invoiceDate": "",
        "currency": "GBP",
        "totalNet": 0.0,
        "totalVat": 0.0,
        "totalGross": 0.0,
    }

    m = re.search(r"Invoice\s*(?:No\.?|Mem\b)[^\d\r\n]{0,16}(\d{7,12})", text, re.I)
    if m:
        header["invoiceNumber"] = m.group(1)
    if not header["invoiceNumber"]:
        # The number is also printed below the payment barcode; useful when the
        # black header label is lost by OCR.
        candidates = re.findall(r"(?m)^\s*(\d{9})\s*$", text)
        candidates += re.findall(r"Trolley\s+Count\s+Summary[^\r\n]*[\r\n]+[^\r\n]*?\b(\d{9})\b", text, re.I)
        candidates += re.findall(r"ID\s+Cases\s+Singles\s+Ex\s+VAT\s+(\d{9})\b", text, re.I)
        if candidates:
            header["invoiceNumber"] = candidates[-1]
    m = re.search(r"Date\s*/?\s*Time\s*[:|]?\s*([0-3]?\d[-/]\d{1,2}[-/]\d{4})", text, re.I)
    if m:
        header["invoiceDate"] = _iso_date(m.group(1))
    if not header["invoiceDate"]:
        m = re.search(r"\b([0-3]?\d[-/]\d{1,2}[-/]\d{4})\b", text[:1000])
        if m:
            header["invoiceDate"] = _iso_date(m.group(1))
    m = re.search(r"Customer\s*no\.?\s*[:|]?\s*(\d{5,12})", text, re.I)
    if m:
        header["customerNo"] = m.group(1)

    lines = [re.sub(r"\s+", " ", line).strip(" |") for line in text.splitlines()]
    for index, line in enumerate(lines):
        if re.search(r"Customer\s*no", line, re.I):
            tail = re.split(r"Customer\s*no\.?\s*[:|]?\s*\d+(?:\s*\([^)]*\))?", line, flags=re.I)[-1].strip(" :-|")
            candidates = ([tail] if tail else []) + lines[index + 1:index + 4]
            for candidate in candidates:
                candidate = candidate.strip(" :-|")
                candidate = re.sub(r"^BESTWAY(?:\s+|$)", "", candidate, flags=re.I).strip(" :-|\\")
                if (
                    len(candidate) >= 3
                    and not re.search(r"^(VAT|CONTACT|TELEPHONE|EMAIL|NONE)\b", candidate, re.I)
                    and not re.search(r"\b(ME\d|ROAD|STREET|CENTRE|SHOPPING|KENT|LONDON)\b", candidate, re.I)
                ):
                    header["customerName"] = candidate
                    header["tradingName"] = candidate
                    break
            break

    # The invoice summary/footer is authoritative. OCR may use 'Value (Ex VAT)'.
    matches = re.findall(r"Value\s*\(\s*Ex\s*VAT\s*\)\s*[:|]?\s*£?\s*([0-9][0-9,.]*)", text, re.I)
    if matches:
        header["totalNet"] = _money(matches[-1])
    matches = re.findall(r"Total\s+value\s*\(\s*Ex\s*VAT\s*\)\s*£?\s*([0-9][0-9,.]*)", text, re.I)
    if matches:
        header["totalNet"] = _money(matches[-1])
    matches = re.findall(r"Total\s+due\s*\(\s*Inc\s*VAT\s*\)\s*£?\s*([0-9][0-9,.]*)", text, re.I)
    if matches:
        header["totalGross"] = _money(matches[-1])
    if not header["totalGross"]:
        matches = re.findall(r"\bTotal\s*:\s*£?\s*([0-9][0-9,.]*)", text, re.I)
        if matches:
            header["totalGross"] = _money(matches[-1])
    matches = re.findall(r"(?m)^\s*VAT\s+£?\s*([0-9][0-9,.]*)\s*$", text, re.I)
    if matches:
        header["totalVat"] = _money(matches[-1])
    if header["totalGross"] and header["totalNet"] and not header["totalVat"]:
        header["totalVat"] = round(header["totalGross"] - header["totalNet"], 2)
    return header


def _split_description_size(description: str) -> Tuple[str, str]:
    value = re.sub(r"\s+", " ", description).strip()
    matches = list(
        re.finditer(
            r"(?<!\d)(?:\d+(?:\.\d+)?\s*(?:ML|CL|LTR|L|G|KG)\b|\d+\s*['’]\s*S\b)",
            value,
            re.I,
        )
    )
    if not matches:
        return value, ""
    match = matches[-1]
    size = re.sub(r"\s+", "", match.group(0)).upper().replace("’", "'")
    return (value[:match.start()] + " " + value[match.end():]).strip(), size


def _parse_item_line(line: str) -> Dict[str, Any] | None:
    clean = re.sub(r"\s+", " ", line).strip()
    start = re.match(r"^[|>\s]*(\d{6})\s+([0-9§]{11,14})\s+(.+)$", clean)
    if not start:
        return None
    code, barcode, rest = start.groups()
    barcode = barcode.replace("§", "5")
    pack_match = re.search(r"(?:\b|:)(\d{1,3}|A)\s*[xX]\s*(\d{1,3})\b", rest, re.I)
    if not pack_match:
        return None
    description = rest[:pack_match.start()].strip()
    tail = rest[pack_match.end():].strip()
    # qty, unit price (optional S/C marker), line value, VAT code, optional SRP/POR
    tail_match = re.match(
        r"^([0-9AI|#]{1,4})\s+[^0-9\-]*([0-9][0-9,.]*)(?:\s*\(([SC8])\))?"
        r"\s+[^0-9\-]*([0-9][0-9,.]*)[^0-9]+(1|4|26)\b(.*)$",
        tail,
        re.I,
    )
    if not tail_match:
        # DocTR often preserves the currency columns but drops the one-character
        # quantity or VAT-code cell. Currency anchors still make this layout safe
        # to recover: [qty] £unit-price £line-value [vat-code] [£SRP] [POR].
        currency_match = re.match(
            r"^(?:(\d{1,4})\s+)?£\s*([0-9][0-9,.]*)(?:\s*\(([SC8])\))?"
            r"\s+£\s*([0-9][0-9,.]*)(?:\s+(1|4|26)\b)?(.*)$",
            tail,
            re.I,
        )
        if currency_match:
            qty_s, unit_s, flag, net_s, vat_code, optional = currency_match.groups()
            tail_match = True
        else:
            return None
    else:
        qty_s, unit_s, flag, net_s, vat_code, optional = tail_match.groups()
    qty_s = qty_s or "1"
    vat_code = vat_code or "1"
    qty_clean = re.sub(r"[AI|#]", "1", qty_s.upper())
    # A vertical scan/table border is sometimes recognized as a leading "4"
    # on both adjacent Bestway cells: 1x24 / qty 10 becomes 41x24 / 40.
    # Requiring the same artefact in both fields keeps this correction narrow.
    pack_left_seen = pack_match.group(1).upper()
    if re.fullmatch(r"4\d", pack_left_seen) and re.fullmatch(r"4\d", qty_clean):
        pack_left_seen = pack_left_seen[1:]
        qty_clean = "1" + qty_clean[1:]
    qty = float(qty_clean)
    unit_price = _money(unit_s)
    line_net = _money(net_s)
    # Bestway's printed line value is qty * unit price. OCR occasionally loses
    # a decimal point, but on ruled scans it more commonly reads a vertical
    # border as a leading quantity digit (1 becomes 4, 10 becomes 40). When the
    # printed values imply a clean integer quantity, trust that relationship
    # before changing the authoritative printed line value.
    expected_line_net = round(qty * unit_price, 2)
    if expected_line_net > 0 and abs(line_net - expected_line_net) > max(1.0, expected_line_net * 0.35):
        implied_qty = (line_net / unit_price) if unit_price > 0 else 0.0
        implied_qty_int = int(round(implied_qty)) if implied_qty > 0 else 0
        if (
            1 <= implied_qty_int <= 999
            and abs(implied_qty - implied_qty_int) <= 0.02
        ):
            qty = float(implied_qty_int)
            expected_line_net = round(qty * unit_price, 2)
        if abs(line_net - expected_line_net) > max(1.0, expected_line_net * 0.35):
            line_net = expected_line_net
    vat_code = "1" if vat_code == "4" else vat_code
    vat_rate = BESTWAY_VAT_RATES.get(vat_code, 0.0)
    vat_amount = round(line_net * vat_rate / 100.0, 2)
    description, unit_size = _split_description_size(description)
    optional_money = re.findall(r"£?\s*([0-9]+(?:[.,][0-9]{1,2})?)", optional or "")
    por_match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*%", optional or "")
    pack_left_raw = pack_left_seen
    pack_left = 1 if pack_left_raw in {"A", "4"} else int(pack_left_raw)
    pack_right = int(pack_match.group(2))
    # In "4x 5" OCR, the 4 is a ruled-cell artefact and 5 is actually
    # quantity (confirmed by line value / unit price), not pack-right.
    if pack_left_raw == "4" and pack_right > 1 and abs(float(pack_right) - qty) <= 0.01:
        pack_right = 1
    case_pack = pack_left * pack_right
    return {
        "lineNo": None,
        "code": code,
        "barcode": barcode,
        "description": description,
        "casePack": case_pack,
        "pack": f"{pack_left}x{pack_right}",
        "unitSize": unit_size,
        "qty": qty,
        "totalUnits": float(case_pack) * qty,
        "unitPrice": unit_price,
        "flag": ("S" if (flag or "").upper() == "8" else (flag or "").upper()),
        "lineNet": line_net,
        "vatCode": vat_code,
        "vatRate": vat_rate,
        "vatAmount": vat_amount,
        "lineGross": round(line_net + vat_amount, 2),
        "stdRrp": _money(optional_money[0]) if optional_money else 0.0,
        "por": _money(por_match.group(1)) if por_match else 0.0,
        "isVoid": False,
        "raw": clean,
        "rawRow": clean,
    }


def parse_bestway_invoice(text: str) -> Dict[str, Any]:
    """Parse Bestway Wholesale OCR text into DocMate's header/item schema."""
    clean = _clean_text(text)
    header = _extract_header(clean)
    items: List[Dict[str, Any]] = []
    for line in clean.splitlines():
        item = _parse_item_line(line)
        if item:
            item["lineNo"] = len(items) + 1
            items.append(item)

    vat_rows = []
    for vat_code in ("1", "26"):
        group = [item for item in items if item["vatCode"] == vat_code]
        if group:
            goods = round(sum(float(item["lineNet"]) for item in group), 2)
            vat = round(goods * BESTWAY_VAT_RATES[vat_code] / 100.0, 2)
            vat_rows.append({
                "vatCode": vat_code,
                "vatRate": BESTWAY_VAT_RATES[vat_code],
                "goodsAmount": goods,
                "vatAmount": vat,
                "grossAmount": round(goods + vat, 2),
                "_source": "calculated",
            })
    if vat_rows:
        header["vatBreakdown"] = vat_rows
    item_net = round(sum(float(item["lineNet"]) for item in items), 2)
    if not header["totalNet"] and item_net:
        header["totalNet"] = item_net
    if not header["totalVat"] and header["totalNet"]:
        header["totalVat"] = round(header["totalNet"] * 0.2, 2) if all(item["vatCode"] == "1" for item in items) else round(sum(row["vatAmount"] for row in vat_rows), 2)
    if not header["totalGross"] and header["totalNet"]:
        header["totalGross"] = round(header["totalNet"] + header["totalVat"], 2)
    # A stray line/department value can be captured by the loose "Total:"
    # fallback on noisy scans. Invoice gross must reconcile to printed net + VAT.
    if header["totalNet"] > 0 and header["totalVat"] >= 0:
        expected_gross = round(header["totalNet"] + header["totalVat"], 2)
        if header["totalGross"] <= 0 or abs(header["totalGross"] - expected_gross) > 0.10:
            header["totalGross"] = expected_gross
    return {"header": header, "items": items}

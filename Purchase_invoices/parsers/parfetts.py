from __future__ import annotations
import re
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

# Extracted from legacy.py

PARFETTS_VAT_MAP_DEFAULT = {
    "A": 20.0,
    "Z": 0.0,
    "R": 5.0,
    "B": 20.0,
}


def _safe_float(x: object, default: float = 0.0) -> float:
    if x is None:
        return default
    try:
        if isinstance(x, str):
            s = re.sub(r"[^0-9.\-]+", "", x.strip().replace(",", ""))
            m = re.search(r"-?\d+(?:\.\d+)?", s)
            return float(m.group(0)) if m else default
        return float(x)
    except Exception:
        return default


def _ddmmyyyy_to_iso(s: str) -> str:
    try:
        return dt.datetime.strptime((s or "").strip(), "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        return (s or "").strip()


def _title_case(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).title()


def _parfetts_calc_por(std_rrp: float, vat_rate: float, unit_price_case: float, case_pack: int) -> float:
    try:
        if std_rrp <= 0 or unit_price_case <= 0 or case_pack <= 0:
            return 0.0
        sell_ex_vat = float(std_rrp)
        if vat_rate and vat_rate > 0:
            sell_ex_vat = sell_ex_vat / (1.0 + (float(vat_rate) / 100.0))
        cost_unit = float(unit_price_case) / float(case_pack)
        if sell_ex_vat <= 0:
            return 0.0
        return round(((sell_ex_vat - cost_unit) / sell_ex_vat) * 100.0, 2)
    except Exception:
        return 0.0


def _parfetts_unit_size_before(lines: List[str], idx: int, lookback: int = 6) -> str:
    for k in range(1, lookback + 1):
        j = idx - k
        if j < 0:
            break
        s = (lines[j] or "").strip()
        if not s:
            continue
        m = re.search(r"\b(\d+(?:\.\d+)?)(?:\s*)(ML|CL|L|LT|LTR|G|GM|KG|OZ)\b", s, flags=re.I)
        if m:
            unit = m.group(2).upper()
            if unit == "L":
                unit = "LTR"
            return f"{m.group(1)}{unit}".replace(" ", "")
        m = re.search(r"\b(\d+)\s*(PK|PACK|PC|PCS)\b", s, flags=re.I)
        if m:
            return f"{m.group(1)}{m.group(2).upper()}".replace("PACK", "PK")
        m = re.search(r"\b(\d+)\s*'S\b", s, flags=re.I)
        if m:
            return f"{m.group(1)}S"
    return ""


def _parfetts_clean_compact_description(desc: str, std_rrp: float) -> str:
    d = re.sub(r"\s+", " ", re.sub(r"[^\x20-\x7E]+", "", desc or "")).strip(" -:|")
    d = re.sub(r"^(?:\d{1,2})?[A-Za-z &/]+-\s*", "", d).strip()
    if std_rrp > 0:
        d = re.sub(rf"\b{re.escape(f'{std_rrp:.2f}')}\b\s*$", "", d).strip()
    return d


def _parse_parfetts_compact_text(txt: str) -> List[Dict[str, Any]]:
    """Parse Parfetts PDF text layers where item rows are joined into long strings."""
    if not txt:
        return []

    text = re.sub(r"\s+", " ", txt.replace("\xa0", " ")).strip()
    stop = text.upper().find("TOTAL GOODS")
    if stop > 0:
        text = text[:stop]

    row_pat = re.compile(
        r"(?P<unit>(?:\d+(?:\.\d+)?\s*(?:ML|LTR|LT|CL|G|GM|KG|S|PC|PCS|PK)|\d+'S|CHARGE)\s+)?"
        r"(?P<code>\d{4,7})"
        r"(?P<desc>.{2,120}?)"
        r"(?P<sale_a>\d{1,3})\s*x\s*(?P<sale_b>\d{1,3})\s+"
        r"(?P<qty>\d{1,4})\s+"
        r"(?P<unit_price>\d{1,4}(?:,\d{3})*\.\d{2})(?:\s*[dD])?\s*"
        r"(?P<vat>P?[AZRBX])\s+"
        r"(?P<line_net>\d{1,5}(?:,\d{3})*\.\d{2})\s+"
        r"(?P<qty_ord>\d{1,4})\s+"
        r"(?P<qty_ord_dec>\d{1,4}\.\d{2})\s+"
        r"(?P<std_rrp>\d{1,4}\.\d{2})\s+"
        r"(?P<gm>\d{1,3}\.\d{2})",
        flags=re.I,
    )

    def _valid_match(m: re.Match) -> bool:
        if m.start() > 0 and (text[m.start() - 1].isdigit() or text[m.start() - 1] in ".,"):
            return False
        desc = (m.group("desc") or "").strip()
        up = desc.upper()
        bad_tokens = ("ORDER NO", "DATE OF INVOICE", "CUSTOMER ACCOUNT", "VAT NO", "PAGE ", "SUBTOTAL", "TOTAL GOODS")
        if any(tok in up for tok in bad_tokens):
            return False
        if re.search(r"\b\d{4,7}[A-Z]", desc) or re.search(r"\b\d{4,7}\s+[A-Z]", desc):
            return False
        return True

    items: List[Dict[str, Any]] = []
    pos = 0
    line_no = 1
    while True:
        m = row_pat.search(text, pos)
        if not m:
            break
        if not _valid_match(m):
            pos = m.start() + 1
            continue

        vat_code = (m.group("vat") or "").strip().upper()
        if vat_code.startswith("P") and len(vat_code) == 2:
            vat_code = vat_code[1:]
        sale_a = int(m.group("sale_a") or 0)
        sale_b = int(m.group("sale_b") or 0)
        case_pack = sale_a * sale_b
        qty = _safe_float(m.group("qty"))
        unit_price = _safe_float(m.group("unit_price"))
        line_net = _safe_float(m.group("line_net"))
        std_rrp = _safe_float(m.group("std_rrp"))
        vat_rate = float(PARFETTS_VAT_MAP_DEFAULT.get(vat_code, 0.0))
        vat_amount = round(line_net * vat_rate / 100.0, 2) if vat_rate else 0.0
        unit_size = re.sub(r"\s+", "", (m.group("unit") or "")).upper().replace("LT", "LTR")

        items.append({
            "lineNo": line_no,
            "code": (m.group("code") or "").strip(),
            "description": _parfetts_clean_compact_description(m.group("desc") or "", std_rrp),
            "casePack": int(case_pack or 0),
            "unitSize": unit_size,
            "qty": float(qty),
            "totalUnits": float(qty * float(case_pack or 0)),
            "unitPrice": float(unit_price),
            "lineNet": float(line_net),
            "vatCode": vat_code,
            "vatRate": float(vat_rate),
            "vatAmount": float(vat_amount),
            "lineGross": float(round(line_net + vat_amount, 2)),
            "stdRrp": float(std_rrp),
            "por": float(_parfetts_calc_por(std_rrp, vat_rate, unit_price, case_pack)),
            "isVoid": False,
        })
        line_no += 1
        pos = m.end()

    return items

def _extract_parfetts_vat_breakdown(text: str) -> Tuple[float, float, List[Dict[str, Any]]]:
    """
    Extract Parfetts VAT summary box (A/Z) as rows:
      [{"vatCode":"A","goodsAmount":2666.88,"vatAmount":533.38}, ...]
    Returns (net_total, vat_total, rows).

    Robust to PDF text-layer quirks where columns may be split across lines or include pipe characters.
    """
    money_pat = r"[-]?\d{1,3}(?:,\d{3})*(?:\.\d{2})"
    rows: List[Dict[str, Any]] = []
    try:
        block = (text or "")
        block_norm = block.replace("|", " ")
        lines = [ln.strip() for ln in block_norm.splitlines() if ln.strip()]

        # locate VAT summary header region
        start_idx = None
        for i, ln in enumerate(lines):
            u = ln.upper()
            if "VAT CODE" in u:
                start_idx = i
                break
            if u.replace(" ", "") in {"VATCODE", "VATCODEGOODSAMOUNTVATAMOUNT"}:
                start_idx = i
                break
        if start_idx is None:
            return (0.0, 0.0, [])

        window = lines[start_idx : min(len(lines), start_idx + 140)]
        window_text = "\n".join(window)

        # Amounts may appear on same line or split across newline(s)
        pat = re.compile(rf"\b([AZ])\b\s+({money_pat})\s*(?:\n\s*|\s+)({money_pat})", flags=re.I)
        for m in pat.finditer(window_text):
            vc = (m.group(1) or "").strip().upper()
            goods = _safe_float(m.group(2))
            vat = _safe_float(m.group(3))
            if vc in {"A", "Z"}:
                rows.append({"vatCode": vc, "goodsAmount": float(goods), "vatAmount": float(vat)})

        # de-dupe by vatCode (keep first)
        seen = set()
        dedup: List[Dict[str, Any]] = []
        for r in rows:
            vc = (r.get("vatCode") or "").strip().upper()
            if not vc or vc in seen:
                continue
            seen.add(vc)
            dedup.append(r)

        net_total = round(sum(_safe_float(r.get("goodsAmount")) for r in dedup), 2)
        vat_total = round(sum(_safe_float(r.get("vatAmount")) for r in dedup), 2)
        return (net_total, vat_total, dedup)
    except Exception:
        return (0.0, 0.0, [])

def _parfetts_extract_header(txt: str) -> Dict[str, Any]:
    """
    Parfetts (Aintree) invoice header + totals extraction.

    Totals priority:
      - Prefer VAT summary box (Vat Code table) when available (most reliable)
      - Else use footer totals (supports layouts where labels are stacked then values are stacked)
      - Gross validated against net+vat+delivery to prevent drift (e.g., 533.60 vs 533.38)
    """
    header: Dict[str, Any] = {
        "supplierName": "Parfetts",
        "customerName": "",
        "invoiceNumber": "",
        "invoiceDate": "",
        "currency": "GBP",
        "totalNet": 0.0,
        "totalVat": 0.0,
        "totalGross": 0.0,
        "deliveryCharge": 0.0,
        "vatBreakdown": [],
    }

    try:
        t = (txt or "")
        up = t.upper()

        # Invoice number
        m = re.search(r"INVOICE\s*NO\s*[:\s]*([0-9]{6,})", up)
        if m:
            header["invoiceNumber"] = m.group(1).strip()


        # Customer name (Parfetts uses (CR) <NAME> ...; be tolerant to multiple bracketed tags like (DEL)(P))
        customer = ""
        m = re.search(r"(?m)^\s*\(CR\)\s*([^\n\r]{3,120})\s*$", t, flags=re.I)
        if not m:
            m = re.search(r"(?m)^\s*CR\)\s*([^\n\r]{3,120})\s*$", t, flags=re.I)
        if not m:
            m = re.search(r"(?m)^\s*\(CR\s*([^\n\r]{3,120})\s*$", t, flags=re.I)
        if not m:
            m = re.search(r"(?m)^\s*\[CR\]\s*([^\n\r]{3,120})\s*$", t, flags=re.I)
        if m:
            # take the first line after (CR), remove common tags
            customer = m.group(1).strip()
            customer = re.sub(r"\(\s*(?:DEL|P)\s*\)", "", customer, flags=re.I)
            customer = re.sub(r"\s{2,}", " ", customer).strip(" -:|")
        if not customer:
            m = re.search(r"\(CR\)\s*([A-Z0-9&' \-\.]+?)(?:\((?:DEL|P)\)\s*){0,3}(?:\s+\d+\s+[A-Z]|\s+ORDER\s+NO|\s+INVOICE\s+NO|\n)", up)
            if m:
                customer = m.group(1).strip()
        if not customer:
            m = re.search(r"\(CR\)\s*(.{3,120}?)(?:\(\s*DEL\s*\)|\(\s*P\s*\)|TEL\s*:|UNITSIZE|DATE\s+OF\s+ORDER)", up, flags=re.I)
            if m:
                customer = re.sub(r"\(\s*(?:DEL|P)\s*\)", "", m.group(1), flags=re.I).strip(" -:|")
        if customer:
            header["customerName"] = _title_case(customer)

        # Invoice date
        # Prefer explicit "Date of Invoice:" (most reliable), else fallback to the first dd/mm/yyyy time near header, else Date Printed.
        date_str = ""
        m = re.search(r"DATE\s+OF\s+INVOICE[:\s]*([0-9]{2}/[0-9]{2}/[0-9]{4})", up)
        if not m:
            m = re.search(r"DATE\s+OF\s+INVO[1I]CE[:\s]*([0-9]{2}/[0-9]{2}/[0-9]{4})", up)
        if m:
            date_str = m.group(1)
        if not date_str:
            m = re.search(r"\b([0-9]{2}/[0-9]{2}/[0-9]{4})\s+[0-9]{2}:[0-9]{2}:[0-9]{2}\b", t)
            if m:
                date_str = m.group(1)
        if not date_str:
            m = re.search(r"DATE\s+PRINTED[:\s]*([0-9]{2}/[0-9]{2}/[0-9]{4})", up)
            if m:
                date_str = m.group(1)
        if date_str:
            header["invoiceDate"] = _ddmmyyyy_to_iso(date_str)

        money = r"[-]?\d{1,3}(?:,\d{3})*(?:\.\d{2})"

        def _money_after(label_re: str) -> float:
            m2 = re.search(rf"{label_re}(?:\s*[:Â£]?\s*)(?:\n\s*|\s+)*({money})", t, flags=re.I)
            if not m2:
                return 0.0
            return _safe_float(m2.group(1))

        # Basic footer reads (works when value appears after label)
        net_f = _money_after(r"TOTAL\s+GOODS")
        vat_f = _money_after(r"TOTAL\s+VAT")
        gross_f = _money_after(r"TOTAL\s+INVOICE")
        deliv_f = _money_after(r"DELIVERY\s+CHARGE")

        # Handle stacked-label layout: labels first, values afterwards
        if (net_f <= 0 or vat_f <= 0 or gross_f <= 0) and ("TOTAL GOODS" in up and "TOTAL VAT" in up and "TOTAL INVOICE" in up):
            mblk = re.search(r"TOTAL\s+GOODS.*?TOTAL\s+VAT.*?TOTAL\s+INVOICE", up, flags=re.S)
            if mblk:
                tail = t[mblk.end(): mblk.end() + 600]
                nums = [ _safe_float(x) for x in re.findall(money, tail) ]
                # Typical sequence: net, vat, (delivery), gross
                if len(nums) >= 2 and net_f <= 0:
                    net_f = nums[0]
                if len(nums) >= 2 and vat_f <= 0:
                    vat_f = nums[1]
                # delivery might be present as 3rd number if delivery charge line exists
                if "DELIVERY CHARGE" in tail.upper() and len(nums) >= 4:
                    if deliv_f <= 0:
                        deliv_f = nums[2]
                    if gross_f <= 0:
                        gross_f = nums[3]
                else:
                    if gross_f <= 0 and len(nums) >= 3:
                        gross_f = nums[2]

        # VAT summary breakdown (A/Z) â€” most reliable totals source
        net_v, vat_v, rows_v = _extract_parfetts_vat_breakdown(t)
        if rows_v:
            try:
                for r in rows_v:
                    if isinstance(r, dict) and "_source" not in r:
                        r["_source"] = "invoice"
            except Exception:
                pass
            header["vatBreakdown"] = rows_v

        # Choose totals: prefer VAT breakdown when present
        if rows_v and net_v > 0:
            net = float(net_v)
            vat = float(vat_v)
        else:
            net = float(net_f or 0.0)
            vat = float(vat_f or 0.0)

        delivery = float(deliv_f or 0.0)
        header["deliveryCharge"] = float(round(delivery, 2))

        gross_calc = float(round(net + vat + delivery, 2))
        gross = float(gross_f or 0.0)

        # If footer gross missing or inconsistent, use calculated gross
        if gross <= 0 or abs(gross - gross_calc) > 0.05:
            gross = gross_calc

        header["totalNet"] = float(round(net, 2))
        header["totalVat"] = float(round(vat, 2))
        header["totalGross"] = float(round(gross, 2))

    except Exception:
        pass

    return header

def _parse_parfetts_purchase_invoice(txt: str) -> List[Dict[str, Any]]:
    """Deterministic parser for Parfetts purchase invoices.

    Notes:
    - Parfetts uses a 'Sale Unit' style of 'A' then 'x B' (e.g., '6' then 'x 1', or '1' then 'x 6').
      We treat casePack as A*B so totalUnits = qty * casePack reflects true units.
    - Some PDFs split qty and unit price across two lines (qty line then price line), sometimes with a trailing
      letter on the price line (e.g., '7.49 d'). We support both formats.
    """
    lines_raw = [(ln or "").strip() for ln in (txt or "").splitlines()]
    lines = [ln for ln in lines_raw if ln]
    items: List[Dict[str, Any]] = []

    vat_codes = set(["A", "Z", "R", "B", "X"])
    vat_map = PARFETTS_VAT_MAP_DEFAULT

    i = 0
    line_no = 1
    while i < len(lines):
        ln = lines[i]

        # Item code is typically a 4-7 digit line.
        if re.fullmatch(r"\d{4,7}", ln):
            code = ln

            # Unit size: generally appears within the few lines above the code (e.g., 440ML, 75CL, 130G, 8PK).
            unit_size = _parfetts_unit_size_before(lines, i)

            # Description: accumulate until the 'Sale Unit' marker (a number line followed by a line containing 'x').
            desc_parts: List[str] = []
            j = i + 1
            while j < len(lines):
                if re.fullmatch(r"\d{1,4}", lines[j]) and (j + 1) < len(lines) and "x" in lines[j + 1].lower():
                    break
                if re.fullmatch(r"\d{4,7}", lines[j]):
                    break
                if lines[j].lower().startswith("page ") and "of" in lines[j].lower():
                    break
                desc_parts.append(lines[j])
                j += 1

            description = " ".join(desc_parts).strip()

            # Must have sale unit marker
            if not (j < len(lines) and re.fullmatch(r"\d{1,4}", lines[j]) and (j + 1) < len(lines) and "x" in lines[j + 1].lower()):
                i += 1
                continue

            # Sale Unit / casePack: A then 'x B' -> casePack = A*B
            sale_a = int(lines[j])
            mxb = re.search(r"x\s*(\d+)", lines[j + 1], flags=re.I)
            sale_b = int(mxb.group(1)) if mxb else 1
            case_pack = sale_a * sale_b
            j += 2  # skip A and 'x B'

            # Look ahead window (up to 18 lines) for qty/unit price, VAT code, ext price, and RRP/GM.
            k = j
            window: List[str] = []
            while k < len(lines):
                if re.fullmatch(r"\d{4,7}", lines[k]) and window:
                    break
                if lines[k].lower().startswith("subtotal") and window:
                    break
                if lines[k].lower().startswith("page ") and "of" in lines[k].lower():
                    break
                if lines[k].lower().startswith("total ") and window:
                    break
                window.append(lines[k])
                if len(window) >= 18:
                    break
                k += 1

            qty = 0.0
            unit_price = 0.0
            vat_code = ""
            line_net = 0.0
            std_rrp = 0.0

            # Qty + unit price can be either:
            # 1) single line: "2 18.99" (sometimes with trailing letter)
            # 2) split lines: "2" then "18.99" or "18.99 d"
            for w in window:
                mqp = re.match(r"^(\d+)\s+([0-9][0-9,]*\.[0-9]{2})(?:\s*[A-Za-z])?$", w)
                if mqp:
                    qty = float(int(mqp.group(1)))
                    unit_price = _safe_float(mqp.group(2))
                    break

            if qty <= 0.0 and unit_price <= 0.0:
                for idx in range(len(window) - 1):
                    if re.fullmatch(r"\d+", window[idx]) and re.fullmatch(r"[0-9][0-9,]*\.[0-9]{2}(?:\s*[A-Za-z])?$", window[idx + 1]):
                        q = int(window[idx])
                        p = _safe_float(window[idx + 1])
                        if p > 0.0 and 0 <= q < 1000:
                            qty = float(q)
                            unit_price = p
                            break

            # VAT code letter (A/Z/R/B)
            for w in window:
                if re.fullmatch(r"[A-Za-z]", w):
                    wc = w.upper()
                    if wc in vat_codes:
                        vat_code = wc
                        break

            # Ext price / line net: first money value after VAT code (if found) else first money in window.
            start_idx = 0
            if vat_code and vat_code in window:
                start_idx = window.index(vat_code) + 1
            for w in window[start_idx:]:
                if re.fullmatch(r"[0-9][0-9,]*\.[0-9]{2}", w):
                    line_net = _safe_float(w)
                    break

            # RRP/GM line: take first number from last line that contains two money values.
            for w in reversed(window):
                mrrp = re.search(r"([0-9][0-9,]*\.[0-9]{2})\s+([0-9][0-9,]*\.[0-9]{2})", w)
                if mrrp:
                    std_rrp = _safe_float(mrrp.group(1))
                    break

            # If not delivered, zero amounts. (Invoice text says 'you haven't been charged'.)
            if qty <= 0.0:
                unit_price = 0.0
                line_net = 0.0
                if not vat_code:
                    vat_code = "Z"

            vat_rate = float(vat_map.get((vat_code or "").upper(), 0.0))
            vat_amount = round((line_net * vat_rate / 100.0), 2) if vat_rate > 0.0 else 0.0
            line_gross = round(line_net + vat_amount, 2)

            total_units = qty * float(case_pack or 0)

            # Remove trailing RRP token from description if present (e.g., "... 2.50")
            if std_rrp > 0.0 and description:
                parts = description.split()
                if parts and re.fullmatch(r"\d+\.\d{2}", parts[-1]) and abs(_safe_float(parts[-1]) - std_rrp) < 0.001:
                    description = " ".join(parts[:-1]).strip()

            por = _parfetts_calc_por(std_rrp, vat_rate, unit_price, case_pack)

            items.append({
                "lineNo": line_no,
                "code": code,
                "description": description,
                "casePack": int(case_pack or 0),
                "unitSize": unit_size,
                "qty": float(qty),
                "totalUnits": float(total_units),
                "unitPrice": float(unit_price),
                "lineNet": float(line_net),
                "vatCode": vat_code,
                "vatRate": float(vat_rate),
                "vatAmount": float(vat_amount),
                "lineGross": float(line_gross),
                "stdRrp": float(std_rrp),
                "por": float(por),
                "isVoid": False,
            })
            line_no += 1

            i = k
            continue

        i += 1

    compact_items = _parse_parfetts_compact_text(txt)
    if len(compact_items) > len(items):
        return compact_items

    return items


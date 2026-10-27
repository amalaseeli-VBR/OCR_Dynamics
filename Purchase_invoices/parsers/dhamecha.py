from __future__ import annotations

import datetime
import re
from typing import Any, Dict, List, Optional

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        cleaned = re.sub(r"[^0-9.\-]", "", str(value).replace(",", ""))
        return float(cleaned) if cleaned not in {"", "-", ".", "-."} else default
    except (TypeError, ValueError):
        return default


def _round2(value: Any) -> float:
    return round(_safe_float(value), 2)


def _collapse_spaces(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _looks_like_number(value: Any) -> bool:
    return bool(re.fullmatch(r"[£\s]*-?\d+(?:[.,]\d+)?[P%]?", str(value or "").strip(), re.I))


def looks_like_dhamecha_invoice(text: str) -> bool:
    upper = str(text or "").upper()
    return bool(
        "DHAMECHA" in upper
        or "927135230" in upper
        or "XRAW00000102826" in upper
        or (
            "ITEM #" in upper
            and "PRODUCT DESCRIPTION" in upper
            and "UOP" in upper
            and "UOS" in upper
            and "POR" in upper
        )
        or (
            "TOTAL GOODS" in upper
            and "TOTAL INCL VAT" in upper
            and bool(re.search(r"(?m)^\s*\d{3,9}\b", str(text or "")))
        )
    )


# Extracted from legacy.py

def _dhamecha_parse_vat_code_rate_table(doc_text: str) -> dict[str, float]:
    """Parse Dhamecha VAT code->rate table.

    Handles both formats commonly seen in PDFs/OCR:
      1) Multi-line header:
         Code
         Rate
         A
         20
      2) Single-line rows:
         Code  Rate
         A     20
         B     5
         Z     0
    """
    mapping: dict[str, float] = {}
    lines = [ln.strip() for ln in (doc_text or "").splitlines()]

    # Find table header (either 'Code' then 'Rate' on next line, or 'Code Rate' on one line)
    start_idx = None
    for i, ln in enumerate(lines):
        lo = ln.lower()
        if "code" in lo and "rate" in lo:
            start_idx = i + 1
            break
        # Sometimes header appears as two consecutive lines: 'Code' then 'Rate'
        if lo == "code" and i + 1 < len(lines) and lines[i + 1].strip().lower() == "rate":
            start_idx = i + 2
            break

    if start_idx is None:
        return mapping

    # Parse subsequent lines until we hit a blank run or something clearly outside the table.
    blank_run = 0
    for j in range(start_idx, len(lines)):
        ln = lines[j]
        if not ln:
            blank_run += 1
            if blank_run >= 2:
                break
            continue
        blank_run = 0

        # Single-line row like: 'A 20' or 'A: 20%' or 'A    20.0'
        m = re.match(r'^([A-Z])\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*%?$', ln)
        if m:
            mapping[m.group(1)] = float(m.group(2))
            continue

        # Multi-line variant: code on its own line then rate on next line
        if re.fullmatch(r"[A-Z]", ln) and j + 1 < len(lines):
            nxt = lines[j + 1].strip()
            m2 = re.match(r'^(\d+(?:\.\d+)?)\s*%?$', nxt)
            if m2:
                mapping[ln] = float(m2.group(1))
                continue

        # Stop if we start seeing non-table content
        if len(mapping) >= 1 and re.search(r'(invoice|total|vat|gross|net|amount)', ln, re.I):
            break

    return mapping

def _dhamecha_parse_purchase_invoice(txt: str) -> Dict[str, Any]:
    """
    Dhamecha purchase invoice parser for Local OCR / PDF text layer.

    This supplier PDF often contains a legal disclaimer at the top which can break naive supplier detection.
    We parse from the PDF text layer when available and fall back to OCR text.
    """
    up = (txt or "").upper()
    header = {
        "supplierName": "Dhamecha Foods Limited",
        "customerName": "",
        "invoiceNumber": "",
        "invoiceDate": "",
        "currency": "GBP",
        "totalNet": 0.0,
        "totalVat": 0.0,
        "totalGross": 0.0,
    }
    items: List[Dict[str, Any]] = []

    # Invoice number
    m = re.search(r"(?im)\bINVOICE\s*[:#]?\s*([0-9]{5,10})\b", txt or "")
    if m:
        header["invoiceNumber"] = m.group(1).strip()

    # Date (prefer "16 Jan 2026" style)
    m = re.search(r"(?im)\bDate/Time\s*\n\s*([0-9]{1,2}\s+[A-Za-z]{3}\s+[0-9]{4})", txt or "")
    if not m:
        m = re.search(r"(?im)\b([0-9]{1,2}\s+[A-Za-z]{3}\s+[0-9]{4}),\s*[0-9]{1,2}:[0-9]{2}\b", txt or "")
    if m:
        try:
            dt = datetime.datetime.strptime(m.group(1).strip(), "%d %b %Y")
            header["invoiceDate"] = dt.date().isoformat()
        except Exception:
            header["invoiceDate"] = m.group(1).strip()

    # Totals
    def _money(pat: str) -> float:
        mm = re.search(pat, txt or "", flags=re.I | re.M)
        if not mm:
            return 0.0
        return _safe_float(mm.group(1))

    # These appear near the bottom of the Dhamecha invoice
    header["totalNet"] = _money(r"(?im)\bTotal Goods:\s*£\s*([0-9]+(?:\.[0-9]{1,2})?)")
    header["totalVat"] = _money(r"(?im)^\s*VAT:\s*£\s*([0-9]+(?:\.[0-9]{1,2})?)\s*$")
    header["totalGross"] = _money(r"(?im)\bTotal incl VAT:\s*£\s*([0-9]+(?:\.[0-9]{1,2})?)")

    # Customer name: capture the trading name line above address (e.g. DAILY DELIGHTS STORE)
    # Look for a likely uppercase business name line followed by a street number.
    m = re.search(r"(?m)\n([A-Z][A-Z0-9 &'\-\/]{3,})\n\s*\d{1,4}\s+[A-Z]", txt or "")
    if m:
        header["customerName"] = _collapse_spaces(m.group(1).strip().title())

    # Line items: parse lines that start with item number then description + columns ending with POR% and VAT code.
    # The PDF text layer prints items as single lines; OCR may vary but usually keeps tokens.
    # Line items (Dhamecha):
    # The extracted text (PDF text layer OR OCR) often appears as a vertical table:
    #   827873
    #   DESCRIPTION (may be 1–3 lines)
    #   20's
    #   -
    #   10
    #   1
    #   113.75
    #   113.75P
    #   14.70
    #   7.10%
    #   A
    #
    # So we parse by blocks: itemNo line → find next POR% line → VAT code line.
    vat_map = {"A": 20.0, "B": 5.0, "Z": 0.0, "R": 5.0}

    lines = [ (ln or "").replace("\xa0", " ").strip() for ln in (txt or "").splitlines() ]

    # --- Horizontal table parser (preferred when PDF text layer keeps rows on one line) ---
    def _parse_horizontal_rows(_lines: List[str]) -> List[Dict[str, Any]]:
        start = -1
        for idx, ln in enumerate(_lines):
            u = (ln or "").upper()
            if "ITEM" in u and "PRODUCT" in u and "VAT" in u and "VALUE" in u:
                start = idx + 1
                break
        if start < 0:
            return []
        out: List[Dict[str, Any]] = []
        for ln in _lines[start:]:
            if not (ln or "").strip():
                continue
            u = ln.upper()
            if "TOTAL GOODS" in u or "VALUE OF GOODS" in u or u.strip().startswith("VAT:") or "TOTAL INCL VAT" in u:
                break
            # Typical row tokens:
            # 520558 PRAWN CRACKERS 250G  1 12.00 12.00 A
            # itemNo desc... UOP UP Qty Value VAT
            toks = (ln or "").split()
            if len(toks) < 6:
                continue
            if not re.fullmatch(r"\d{5,9}", toks[0]):
                continue
            if not re.fullmatch(r"[A-Z]{1,2}", toks[-1]):
                continue
            vat_code = toks[-1].strip().upper()
            value_tok = toks[-2]
            qty_tok = toks[-3]
            unit_tok = toks[-4]
            if not _looks_like_number(value_tok):
                continue
            if not _looks_like_number(qty_tok):
                # sometimes qty is '-' and value still present; skip
                continue
            line_net = _safe_float(value_tok)
            qty = _safe_float(qty_tok)
            unit_price = _safe_float(unit_tok) if _looks_like_number(unit_tok) else 0.0
            desc = " ".join(toks[1:-4]).strip()
            if not desc:
                desc = "Item"
            vat_rate = float(vat_map.get(vat_code, 20.0))
            vat_amount = round(line_net * (vat_rate / 100.0), 2) if vat_rate > 0 else 0.0
            out.append({
                "lineNo": len(out) + 1,
                "code": toks[0],
                "description": _collapse_spaces(desc),
                "casePack": "",
                "unitSize": "",
                "qty": qty,
                "unitPrice": unit_price,
                "lineNet": round(line_net, 2),
                "vatRate": vat_rate,
                "vatAmount": vat_amount,
                "lineGross": round(line_net + vat_amount, 2),
                "stdRrp": 0.0,
                "por": 0.0,
                "vatCode": vat_code,
            })
        return out

    try:
        items = _parse_horizontal_rows(lines)
    except Exception:
        items = []

    # If horizontal parse succeeded, return immediately with header and items.
    if items:
        # If totals missing, infer from items (but keep extracted totals if present)
        if _safe_float(header.get("totalNet")) <= 0:
            header["totalNet"] = round(sum(_safe_float(it.get("lineNet")) for it in items), 2)
        if _safe_float(header.get("totalVat")) <= 0:
            header["totalVat"] = round(sum(_safe_float(it.get("vatAmount")) for it in items), 2)
        if _safe_float(header.get("totalGross")) <= 0:
            header["totalGross"] = round(sum(_safe_float(it.get("lineGross")) for it in items), 2)
        return {"header": header, "items": items}

    # --- Vertical/stacked table parser (fallback) ---

    n = len(lines)
    i = 0

    def _strip_money_token(s: str) -> float:
        return _safe_float((s or "").replace("£", "").replace("P", "").strip())

    def _is_skip_line(s: str) -> bool:
        sl = (s or "").strip().lower()
        if not sl:
            return True
        return sl.startswith((
            "trolley:", "cases:", "singles:", "total:", "sub total:", "inc. vat:",
            "items total:", "code", "rate", "value of goods", "total goods:", "vat:", "total incl",
            "this invoice", "please note", "title in the goods", "page "
        ))

    while i < n:
        s = lines[i]
        if _is_skip_line(s):
            i += 1
            continue

        if not re.fullmatch(r"\d{5,7}", s):
            i += 1
            continue

        item_no = s
        # Look ahead for a POR% line (ends with %) within the next 25 lines.
        por_idx = None
        for j in range(i + 1, min(i + 26, n)):
            sj = lines[j]
            if _is_skip_line(sj):
                continue
            if re.fullmatch(r"\d+(?:\.\d+)?%", sj):
                por_idx = j
                break
        if por_idx is None or por_idx + 1 >= n:
            i += 1
            continue

        vat_code = lines[por_idx + 1].strip().upper()
        if vat_code not in vat_map and not re.fullmatch(r"[A-Z]{1,3}", vat_code):
            i += 1
            continue

        # We expect: ... UOP, UOS, Qty, Price, Ext, RRP, POR%, VAT
        # Therefore indices:
        #   uop = por_idx - 6
        #   uos = por_idx - 5
        #   qty = por_idx - 4
        #   unit = por_idx - 3
        #   ext = por_idx - 2
        #   rrp = por_idx - 1
        try:
            uop = lines[por_idx - 6].strip()
            uos = lines[por_idx - 5].strip()
            qty = _safe_float(lines[por_idx - 4].strip())
            unit = _strip_money_token(lines[por_idx - 3].strip())
            ext = _strip_money_token(lines[por_idx - 2].strip())
            rrp = _strip_money_token(lines[por_idx - 1].strip())
            por = _safe_float(lines[por_idx].replace("%", "").strip())
        except Exception:
            i += 1
            continue

        # Description is everything between item_no and uop (may be multi-line)
        desc_lines = []
        for k in range(i + 1, por_idx - 6):
            if _is_skip_line(lines[k]):
                continue
            desc_lines.append(lines[k])
        desc = _collapse_spaces(" ".join(desc_lines)).strip()

        vat_rate = float(vat_map.get(vat_code, 20.0))
        line_net = float(ext)
        vat_amt = _round2(line_net * (vat_rate / 100.0))
        line_gross = _round2(line_net + vat_amt)

        items.append({
            "lineNo": len(items) + 1,
            "code": item_no,
            "description": desc,
            "casePack": "",   # Dhamecha doesn't have a reliable casePack token in this layout
            "unitSize": _collapse_spaces(f"{uop} {uos}").strip(),
            "qty": float(qty),
            "unitPrice": float(unit),
            "lineNet": float(line_net),
            "vatRate": float(vat_rate),
            "vatAmount": float(vat_amt),
            "lineGross": float(line_gross),
            "stdRrp": float(rrp),
            "por": float(por),
            "vatCode": vat_code,
        })

        # Move index past this block to avoid double counting
        i = por_idx + 2
# If totals are missing but we have items, compute totals
    if items and float(header.get("totalNet") or 0.0) <= 0.0:
        net = sum(_safe_float(it.get("lineNet")) for it in items)
        vat = sum(_safe_float(it.get("vatAmount")) for it in items)
        header["totalNet"] = _round2(net)
        header["totalVat"] = _round2(vat)
        header["totalGross"] = _round2(net + vat)

    return {"header": header, "items": items}

def _dhamecha_build_item_map(doc_text: str) -> dict[str, dict]:
    """Build a lightweight map keyed by Dhamecha *Item #* (usually 5–7 digits).

    Why this exists:
      - Dhamecha invoices have the VAT code (A/B/Z) as a single letter in the last column.
      - Cloud extraction can occasionally mis-read that narrow column.
      - The PDF text layer usually contains the correct VAT code per row; we use it as source-of-truth.

    The function supports two text formats:
      1) PDF text-layer "row" lines (most common): each item row is one line starting with the item #,
         and descriptions may wrap onto the next line(s) where the VAT code appears at the end.
      2) OCR tokenised text: item # appears on its own line, with tokens below it.
    """
    _sync_from_legacy()
    lines = [ln.rstrip() for ln in (doc_text or "").splitlines()]
    out: dict[str, dict] = {}

    # ---- A) Row-based parse (PDF text layer) ----
    current_code: str | None = None
    for raw in lines:
        ln = (raw or "").strip()
        if not ln:
            continue

        # Start of an item row: 5–7 digits followed by a space
        m = re.match(r'^(\d{5,7})\s+.+$', ln)
        if m:
            current_code = m.group(1)
            out.setdefault(current_code, {"vatCode": None, "casePack": None})

        # VAT code is a single trailing letter A/B/Z (e.g., "... 0% A" or "... 0.0% Z")
        mvat = re.search(r'\s([ABZ])\s*$', ln, flags=re.IGNORECASE)
        if mvat and current_code:
            out.setdefault(current_code, {"vatCode": None, "casePack": None})
            out[current_code]["vatCode"] = mvat.group(1).upper()

            # Try best-effort casePack parse from the same line (not critical for VAT)
            # Common pattern: "... - 24 1 23.99 23.99P ..."
            mcp = re.search(r'\s-\s*(\d+)\s+\d+\s+\d+(?:\.\d+)?\s+\d+\.\d{2}\b', ln)
            if mcp:
                try:
                    out[current_code]["casePack"] = float(mcp.group(1))
                except Exception:
                    pass

            current_code = None  # done with this row (until next item #)

    # ---- B) Token-based parse (PyMuPDF token stream) ----
    # Fallback for text where item # appears on its own line (common with PyMuPDF text extraction).
    if not out:
        lines2 = [ln.strip() for ln in (doc_text or "").splitlines()]
        code_idxs = [i for i, ln in enumerate(lines2) if re.fullmatch(r"\d{5,7}", ln)]

        for k, idx in enumerate(code_idxs):
            code = lines2[idx]
            end = code_idxs[k + 1] if k + 1 < len(code_idxs) else len(lines2)

            # Stop at end-of-item markers so we don't accidentally pick up VAT codes from the footer VAT table.
            stop = end
            for j in range(idx + 1, end):
                lo = (lines2[j] or "").lower()
                if lo.startswith("trolley:") or lo.startswith("items total") or ("code rate" in lo) or lo.startswith("total goods"):
                    stop = j
                    break

            seg = [s for s in lines2[idx:stop] if s]

            # VAT code (A/B/Z) usually appears as a single-letter token immediately after a percentage token (POR column).
            vat_code = None
            for j in range(len(seg) - 1):
                if re.fullmatch(r"\d+(?:\.\d+)?%", seg[j]):
                    cand = (seg[j + 1] or "").strip().upper()
                    if re.fullmatch(r"[ABZ]", cand):
                        vat_code = cand

            # Fallback: last A/B/Z within the *item segment* (not the whole page).
            if not vat_code:
                for s in seg:
                    if re.fullmatch(r"[ABZ]", s, flags=re.IGNORECASE):
                        vat_code = s.upper()

            # Find first float (unit price typically). Use last 2 ints before it as (casePack, qty)
            unit_price_idx = None
            for j, s in enumerate(seg):
                if re.fullmatch(r"\d+\.\d{2}", s):
                    unit_price_idx = j
                    break

            case_pack = None
            if unit_price_idx is not None:
                ints = []
                for t in seg[:unit_price_idx]:
                    if re.fullmatch(r"\d+", t):
                        ints.append(int(t))
                if len(ints) >= 2:
                    case_pack = float(ints[-2])

            out[code] = {"vatCode": vat_code, "casePack": case_pack}
    return out


def _dhamecha_enrich_header_items(header: dict, items: list[dict], doc_text_hint: str = "") -> None:
    """Dhamecha-specific post-processing: VAT code/rate/amount + casePack fixes.

    Goal:
      - Ensure each line has vatCode (A/B/Z), vatRate (20/5/0) and vatAmount consistent with lineNet
      - Improve VAT Breakdown grouping (depends on vatCode)
      - Keep totals aligned for downstream posting into VBR EPOS / Cloud Reporting
    """
    _sync_from_legacy()
    if not items:
        return

    vat_rates = _dhamecha_parse_vat_code_rate_table(doc_text_hint) or {}
    # Fallback default mapping (Dhamecha typical)
    vat_rates = {**{"A": 20.0, "B": 5.0, "Z": 0.0}, **vat_rates}

    item_map = _dhamecha_build_item_map(doc_text_hint) or {}

    # If header totals strongly imply a single VAT rate (e.g. VAT ≈ 20% of Net),
    # we can safely correct mis-read vatCode/vatRate on lines that came through as Z/UNK.
    enforce_header_vat = False
    dominant_code = None
    try:
        h_net = _safe_float((header or {}).get("totalNet"))
        h_vat = _safe_float((header or {}).get("totalVat"))
        if h_net > 0:
            hdr_rate = (h_vat / h_net) * 100.0
            snap = _nearest_uk_vat_rate(hdr_rate)
            if abs(hdr_rate - snap) <= 0.35:
                enforce_header_vat = True
                dominant_code = "A" if snap == 20.0 else ("B" if snap == 5.0 else "Z")
    except Exception:
        enforce_header_vat = False
        dominant_code = None

    for it in items:
        if not isinstance(it, dict):
            continue

        code = str(it.get("code") or "").strip()

        # 1) VAT Code: from item field, or from vatRate when it contains a code, or from OCR item_map
        vc = str(it.get("vatCode") or "").strip().upper()
        vr_raw = it.get("vatRate")

        if not vc:
            if isinstance(vr_raw, str) and re.fullmatch(r"[ABZ]", vr_raw.strip().upper()):
                vc = vr_raw.strip().upper()
            elif code and code in item_map and item_map[code].get("vatCode"):
                vc = str(item_map[code]["vatCode"] or "").strip().upper()

        # If we have an item_map VAT code from the invoice text, prefer it over Cloud guesses
        if code and code in item_map and item_map[code].get("vatCode"):
            map_vc = str(item_map[code].get("vatCode") or "").strip().upper()
            if map_vc in ("A", "B", "Z") and map_vc != vc:
                vc = map_vc

        if vc:
            it["vatCode"] = vc

        # 2) casePack fallback
        if (not it.get("casePack") or _safe_float(it.get("casePack")) == 0) and code and code in item_map:
            cp = item_map[code].get("casePack")
            if cp:
                it["casePack"] = cp

        # 3) VAT Rate: normalise numeric; if non-numeric/blank/zero but vatCode exists, use mapping
        vr_norm = _normalise_vat_rate(vr_raw)
        if (vr_norm == 0.0) and vc:
            vr_norm = float(vat_rates.get(vc, 0.0))

        
        # Dhamecha hard-rule:
        # If vatCode exists, vatRate MUST match the supplier's code->rate mapping.
        # This prevents a common local-OCR/AI issue where VAT *amount* (e.g. 7.10, 10.80)
        # is mistakenly placed into the vatRate column.
        if vc and vc in vat_rates:
            expected = float(vat_rates.get(vc, 0.0))
            # If rate is clearly not the expected band, snap to expected (tolerance 0.35%)
            if expected >= 0 and abs(vr_norm - expected) > 0.35:
                vr_norm = expected
        else:
            # If we still don't have a code, but rate looks non-UK, snap to nearest UK VAT band.
            # (Dhamecha invoices in our scope use 0/5/20)
            if vr_norm not in (0.0, 5.0, 20.0) and abs(vr_norm - 0.0) > 0.35 and abs(vr_norm - 5.0) > 0.35 and abs(vr_norm - 20.0) > 0.35:
                vr_norm = _nearest_uk_vat_rate(vr_norm)
# If vatCode missing but vatRate is clear, infer vatCode
        if (not vc) and (abs(vr_norm - 20.0) < 0.01):
            it["vatCode"] = "A"
            vc = "A"
        elif (not vc) and (abs(vr_norm - 5.0) < 0.01):
            it["vatCode"] = "B"
            vc = "B"
        elif (not vc) and (abs(vr_norm - 0.0) < 0.01):
            it["vatCode"] = "Z"
            vc = "Z"

        # Header-driven correction (Dhamecha): if the header implies a single VAT band,
        # upgrade mis-read Z/UNK lines to that band so VAT calculates and reconciliation matches.
        try:
            if enforce_header_vat and dominant_code and ((not vc) or vc in ("UNK", "Z")):
                ln = _safe_float(it.get("lineNet"))
                va = _safe_float(it.get("vatAmount"))
                if dominant_code != "Z" and ln > 0 and abs(va) < 0.01:
                    vc = dominant_code
                    it["vatCode"] = vc
                    vr_norm = float(vat_rates.get(vc, vr_norm))
        except Exception:
            pass

        it["vatRate"] = vr_norm

        # 4) VAT Amount and Gross: enforce consistency when rate > 0
        try:
            line_net = _safe_float(it.get("lineNet"))
            vat_amt = _safe_float(it.get("vatAmount"))
            if line_net > 0 and vr_norm > 0:
                calc_vat = round(line_net * (vr_norm / 100.0), 2)
                # Overwrite if missing/zero OR clearly inconsistent (tolerance 5p)
                if (it.get("vatAmount") is None) or (abs(vat_amt - calc_vat) > 0.05):
                    it["vatAmount"] = calc_vat
                # Ensure gross
                it["lineGross"] = round(line_net + _safe_float(it.get("vatAmount")), 2)
            else:
                # Zero-rated: keep VAT at 0
                if it.get("vatAmount") in (None, "", "0", 0, 0.0):
                    it["vatAmount"] = 0.0
                # Ensure gross at least equals net
                if _safe_float(it.get("lineGross")) == 0 and line_net != 0:
                    it["lineGross"] = round(line_net + _safe_float(it.get("vatAmount")), 2)
        except Exception:
            pass

    # Recalculate derived fields (totalUnits, unitCost, por, etc.)
    items[:] = _recalc_items(items)

    # Final pass: if vatCode still missing but rate is known, infer A/B/Z
    for it in items:
        try:
            vc = str(it.get("vatCode") or "").strip().upper()
            vr = _normalise_vat_rate(it.get("vatRate"))
            if not vc:
                if abs(vr - 20.0) < 0.01:
                    it["vatCode"] = "A"
                elif abs(vr - 5.0) < 0.01:
                    it["vatCode"] = "B"
                elif abs(vr - 0.0) < 0.01:
                    it["vatCode"] = "Z"
        except Exception:
            continue

    items[:] = _recalc_items(items)

    # Supplier name normalisation + totals safety
    if header is not None:
        header.setdefault("supplierName", "Dhamecha Cash & Carry Ltd")
        try:
            if _safe_float(header.get("totalNet")) <= 0:
                header["totalNet"] = round(sum(_safe_float(it.get("lineNet")) for it in items), 2)
            if _safe_float(header.get("totalVat")) <= 0:
                header["totalVat"] = round(sum(_safe_float(it.get("vatAmount")) for it in items), 2)
            if _safe_float(header.get("totalGross")) <= 0:
                header["totalGross"] = round(_safe_float(header.get("totalNet")) + _safe_float(header.get("totalVat")), 2)
        except Exception:
            pass


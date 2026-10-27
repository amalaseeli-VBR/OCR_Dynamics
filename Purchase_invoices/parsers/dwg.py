from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Optional, Tuple

def _sync_from_legacy() -> None:
    from docmate import legacy as L
    g = globals()
    for k in dir(L):
        if k.startswith("__"):
            continue
        if k in g:
            continue
        g[k] = getattr(L, k)


# Extracted from legacy.py

def _dwg_extract_header(txt: str) -> Dict[str, Any]:
    """
    Drinks Wholesale Group (DWG) header extractor (best-effort from OCR/text).
    """
    t = txt or ""
    header: Dict[str, Any] = {
        "supplierName": "Drinks Wholesale Group Ltd",
        "customerName": "",
        "invoiceNumber": "",
        "invoiceDate": "",
        "currency": "GBP",
        "totalNet": 0.0,
        "totalVat": 0.0,
        "totalGross": 0.0,
    }

    # Invoice number
    m = re.search(r"\bInvoice\s*No\s*[:#]?\s*(INV[-\s]*\d{6,})\b", t, flags=re.IGNORECASE)
    if m:
        header["invoiceNumber"] = m.group(1).replace(" ", "").upper()
    else:
        m2 = re.search(r"\bINV[-\s]*\d{6,}\b", t, flags=re.IGNORECASE)
        if m2:
            header["invoiceNumber"] = m2.group(0).replace(" ", "").upper()

    # Date (e.g., 16-Nov-25)
    dm = re.search(r"\bDate\s*[:]?\s*(\d{1,2}-[A-Za-z]{3}-\d{2})\b", t)
    if dm:
        try:
            header["invoiceDate"] = dt.datetime.strptime(dm.group(1), "%d-%b-%y").strftime("%Y-%m-%d")
        except Exception:
            pass

    # Customer: prefer any line containing "FRESHERS" (your main use-case), else first line after "Invoice"
    for ln in t.splitlines():
        s = ln.strip()
        if not s:
            continue
        if "FRESHERS" in s.upper():
            header["customerName"] = s.replace("...", "").strip()
            break

    if not header["customerName"]:
        lines = [l.strip() for l in t.splitlines() if l.strip()]
        try:
            inv_idx = next(i for i,l in enumerate(lines) if l.upper().startswith("INVOICE"))
            for cand in lines[inv_idx+1:inv_idx+10]:
                if cand and not any(x in cand.upper() for x in ["VAT", "COMPANY", "AWRS", "INVOICE", "NO:", "DATE:"]):
                    header["customerName"] = cand
                    break
        except Exception:
            pass

    # Totals (Net/VAT/Gross)
    def _money_after(label_pat: str) -> float:
        try:
            mm = re.search(label_pat, t, flags=re.IGNORECASE)
            if not mm:
                return 0.0
            val = mm.group(1).replace(",", "").strip()
            return float(val)
        except Exception:
            return 0.0

    # Common DWG labels: Net Amount / VAT Amount / Gross Amount
    header["totalNet"] = _money_after(r"\bNet\s*Amount\b[^0-9£\n\r]{0,10}£?\s*([0-9,]+(?:\.[0-9]{2})?)")
    header["totalVat"] = _money_after(r"\bVAT\s*Amount\b[^0-9£\n\r]{0,10}£?\s*([0-9,]+(?:\.[0-9]{2})?)")
    header["totalGross"] = _money_after(r"\bGross\s*Amount\b[^0-9£\n\r]{0,10}£?\s*([0-9,]+(?:\.[0-9]{2})?)")

    # Alternative labels sometimes seen
    if header["totalNet"] <= 0:
        header["totalNet"] = _money_after(r"\bTotal\s*Net\b[^0-9£\n\r]{0,10}£?\s*([0-9,]+(?:\.[0-9]{2})?)")
    if header["totalVat"] <= 0:
        header["totalVat"] = _money_after(r"\bTotal\s*VAT\b[^0-9£\n\r]{0,10}£?\s*([0-9,]+(?:\.[0-9]{2})?)")
    if header["totalGross"] <= 0:
        header["totalGross"] = _money_after(r"\bInvoice\s*Total\b[^0-9£\n\r]{0,10}£?\s*([0-9,]+(?:\.[0-9]{2})?)")

    return header

def _parse_dwg_purchase_invoice(txt: str) -> List[Dict[str, Any]]:
    """
    DWG line parser (local/auto): best-effort extraction from OCR/PDF-text.

    Handles:
      - currency lines with or without '£'
      - VAT codes like 'S/A' on the line
      - PM/PMP RRP tokens inside description (stored into stdRrp)
    """
    t = txt or ""
    items: List[Dict[str, Any]] = []

    def _extract_std_rrp_from_desc(desc: str) -> Tuple[float, str]:
        """Extract PM/PMP price from DWG description (supports pence like 'PM 70P')."""
        if not desc:
            return 0.0, ""
        d = str(desc)

        mm = re.search(
            r"(?<![A-Z0-9])(?P<tok>PMP?|PM)\s*[:\-]?\s*(?:\(\s*)?£?\s*(?P<amt>[0-9]+(?:\.[0-9]{1,2})?)(?P<pence>[pP])?(?:\s*\))?",
            d,
            flags=re.IGNORECASE,
        )
        if not mm:
            mm = re.search(
                r"£?\s*(?P<amt>[0-9]+(?:\.[0-9]{1,2})?)(?P<pence>[pP])?\s*(?P<tok>PMP?|PM)\b",
                d,
                flags=re.IGNORECASE,
            )
        if not mm:
            return 0.0, d.strip()

        raw_amt = (mm.group("amt") or "").strip()
        pence_flag = (mm.group("pence") or "").strip()
        match_txt = mm.group(0) or ""

        try:
            price = float(raw_amt)
        except Exception:
            return 0.0, d.strip()

        has_decimal = "." in raw_amt
        has_pound = "£" in match_txt
        if (pence_flag and not has_decimal) or (not has_decimal and not has_pound and 10 <= price <= 99):
            price = price / 100.0

        d2 = (d[:mm.start()] + d[mm.end():]).strip()
        d2 = re.sub(r"\s{2,}", " ", d2)
        return float(round(price, 2)), d2.strip()

    def _vat_code_from_line(s: str) -> str:
        if re.search(r"\bS\s*/\s*A\b", s, flags=re.IGNORECASE) or re.search(r"\bS/A\b", s, flags=re.IGNORECASE):
            return "S/A"
        if re.search(r"\bZ\b", s, flags=re.IGNORECASE):
            return "Z"
        if re.search(r"\bA\b", s, flags=re.IGNORECASE):
            return "A"
        # Default for DWG: standard (20%)
        return "S/A"

    for ln in t.splitlines():
        s = ln.strip()
        if not s:
            continue
        if re.search(r"\bTOTAL\b|\bINVOICE\s+TOTAL\b|\bPAGE\b|\bSUBTOTAL\b|\bDELIVERY\b", s, flags=re.IGNORECASE):
            continue

        # Extract money values: with £ first, else trailing decimals
        prices: List[float] = []
        if "£" in s:
            try:
                prices = [float(x.replace(",", "")) for x in re.findall(r"£\s*([0-9]+(?:\.[0-9]{2})?)", s)]
            except Exception:
                prices = []

        if not prices:
            decs = re.findall(r"([0-9]+(?:\.[0-9]{2}))\b", s)
            if len(decs) >= 2 and (
                re.match(r"^\s*\d{4,10}\b", s)
                or re.match(r"^\s*\d+\s+[A-Za-z0-9\-]{3,12}\b", s)
                or re.match(r"^\s*[A-Za-z0-9\-]{3,12}\b", s)
                or re.search(r"\bS/A\b|\bS\s*/\s*A\b|\bVAT\b", s, flags=re.IGNORECASE)
            ):
                try:
                    prices = [float(x.replace(",", "")) for x in decs[-3:]]
                except Exception:
                    prices = []

        if not prices:
            continue

        # Heuristic: last is line net; second last is unit price when present
        line_net = prices[-1]
        unit_price = prices[-2] if len(prices) >= 2 else prices[-1]

        # Qty: try to derive from net/unit price
        qty = 1.0
        if unit_price > 0:
            q = line_net / unit_price
            rq = round(q)
            if abs(q - rq) <= 0.05 and rq > 0:
                qty = float(rq)

        # Leading code / optional leading qty (DWG often uses short alphanumeric codes)
        code_val = ""
        m_code = re.match(r"^\s*(?:(\d+)\s+)?([A-Za-z0-9\-]{3,12})\b\s+(.*)$", s)
        rest = s
        if m_code:
            # If a leading number exists it may be the case quantity; keep derived qty unless missing.
            lead_qty = m_code.group(1)
            if (qty in (0, 0.0, None)) and lead_qty:
                try:
                    qty = float(lead_qty)
                except Exception:
                    pass
            code_val = m_code.group(2)
            rest = m_code.group(3)


        vat_code = _vat_code_from_line(s)
        vat_rate = 20.0 if vat_code.upper().replace(" ", "") in ("S/A", "SA", "S") else 0.0

        # Description cleanup
        desc = rest
        desc = re.sub(r"(£\s*[0-9]+(?:\.[0-9]{2})?\s*)+", "", desc).strip()
        desc = re.sub(r"\s+[0-9]+(?:\.[0-9]{2})\b(?:\s+[0-9]+(?:\.[0-9]{2})\b)*\s*$", "", desc).strip()
        desc = re.sub(r"\bS\s*/\s*A\b|\bS/A\b", "", desc, flags=re.IGNORECASE).strip(" -|")

        std_rrp, desc = _extract_std_rrp_from_desc(desc)

        vat_amount = round(line_net * (vat_rate / 100.0), 2)
        line_gross = round(line_net + vat_amount, 2)

        items.append({
            "lineNo": 0,
            "code": code_val,
            "description": desc,
            "casePack": "",
            "unitSize": "",
            "qty": qty,
            "totalUnits": "",
            "unitPrice": round(unit_price, 2),
            "lineNet": round(line_net, 2),
            "vatCode": vat_code,
            "vatRate": vat_rate,
            "vatAmount": vat_amount,
            "lineGross": line_gross,
            "stdRrp": float(std_rrp) if std_rrp else 0.0,
            "por": 0.0,
            "isVoid": False,
        })

    return items

def _dwg_enrich_header_items(header, items, supplier_hint=""):
    """DWG (Drinks Wholesale Group) normalisation.

    Ensures DWG line-items have:
      - vatCode populated (often appears in vatRate field as 'S/A')
      - vatRate numeric (e.g. S/A => 20.0)
      - vatAmount + lineGross calculated when missing
      - totalUnits calculated when possible (qty * casePack)

    Returns (header, items) always.
    """
    _sync_from_legacy()
    header = header or {}
    items = items or []
    # Metrics-safe list (exclude voids if any)
    items_for_metrics = items
    try:
        items_for_metrics = [it for it in items_for_metrics if not (isinstance(it, dict) and it.get('isVoid'))]
    except Exception:
        pass

    s = f"{supplier_hint or ''} {header.get('supplierName') or ''}"
    s_low = s.lower()
    is_dwg = any(k in s_low for k in [
        "dwg",
        "drinks wholesale",
        "drinks wholesale group",
        "drinkswholesale",
        "drinks wholesale group ltd",
    ])
    if not is_dwg:
        return header, items

    # Default VAT code/rate (DWG invoices are typically single-rate 'S/A' = 20%)
    default_code = None
    default_rate = None
    def _as_float_or_none(v):
            """Parse float but keep None for missing/blank."""
            if v is None:
                return None
            if isinstance(v, str):
                s = v.strip()
                if not s or s.lower() in ("none", "null", "nan"):
                    return None
                s = s.replace(",", "").replace("%", "")
                try:
                    return float(s)
                except Exception:
                    return None
            try:
                return float(v)
            except Exception:
                return None

    if re.search(r"\bS/A\b", s, flags=re.I):
        default_code = "S/A"
        default_rate = 20.0

    # Infer from items if possible
    for it in items_for_metrics:
        if not isinstance(it, dict):
            continue
        vc = it.get("vatCode")
        vr = it.get("vatRate")
        if isinstance(vc, str) and re.search(r"\bS/A\b", vc, flags=re.I):
            default_code, default_rate = "S/A", 20.0
            break
        if isinstance(vr, str) and re.search(r"\bS/A\b", vr, flags=re.I):
            default_code, default_rate = "S/A", 20.0
            break

    hdr_net = _safe_float(header.get("totalNet"))
    hdr_vat = _safe_float(header.get("totalVat"))

    if default_rate is None and hdr_net and hdr_vat and hdr_net != 0:
        # Use header ratio as fallback
        default_rate = (hdr_vat / hdr_net) * 100.0

    if default_code is None:
        if default_rate is not None and abs(default_rate - 20.0) < 0.75:
            default_code = "S/A"
        else:
            default_code = "NA"

    if default_rate is None:
        default_rate = 0.0
    def _extract_std_rrp_from_desc(desc: str) -> Tuple[float, str]:
        """Extract DWG PM/PMP (RRP) from a description string.
    
        Supports both pounds and pence formats seen on DWG invoices, e.g.
          - "PM 2.99" / "PM £2.99"
          - "PMP 2.99" / "PMP£2.99"
          - "PM 69P" / "PM 70p" (pence)
          - Occasionally: "£2.99 PMP" / "69P PM" (rare OCR ordering)
    
        Returns (rrp, cleaned_description). If not found, rrp=0.0 and description unchanged.
        """
        if not desc:
            return 0.0, ""
        d = str(desc)
    
        # 1) Token then amount (most common). Allow no-space token->amount (e.g. "PM69P") from OCR.
        mm = re.search(
            r"(?<![A-Z0-9])(?P<tok>PMP?|PM)\s*[:\-]?\s*(?:\(\s*)?£?\s*(?P<amt>[0-9]+(?:\.[0-9]{1,2})?)(?P<pence>[pP])?(?:\s*\))?",
            d,
            flags=re.IGNORECASE,
        )
        # 2) Amount then token (rare ordering)
        if not mm:
            mm = re.search(
                r"£?\s*(?P<amt>[0-9]+(?:\.[0-9]{1,2})?)(?P<pence>[pP])?\s*(?P<tok>PMP?|PM)\b",
                d,
                flags=re.IGNORECASE,
            )
    
        if not mm:
            return 0.0, d.strip()
    
        raw_amt = (mm.group("amt") or "").strip()
        pence_flag = (mm.group("pence") or "").strip()
        match_txt = mm.group(0) or ""
    
        price = None
        try:
            price = float(raw_amt)
        except Exception:
            price = None
        if price is None:
            return 0.0, d.strip()
    
        # Convert pence to pounds when indicated or strongly implied.
        has_decimal = "." in raw_amt
        has_pound = "£" in match_txt
        if (pence_flag and not has_decimal) or (not has_decimal and not has_pound and 10 <= price <= 99):
            price = price / 100.0
    
        d2 = (d[:mm.start()] + d[mm.end():]).strip()
        d2 = re.sub(r"\b(PMP?|PM)\b\s*$", "", d2, flags=re.IGNORECASE).strip()
        d2 = re.sub(r"\s{2,}", " ", d2)
        return float(round(price, 2)), d2
    for it in items:
        if not isinstance(it, dict):
            continue
        # --- Normalise key casing from different engines (defensive) ---
        try:
            for k_src, k_dst in [
                ("casepack", "casePack"),
                ("case_pack", "casePack"),
                ("unitsize", "unitSize"),
                ("unit_size", "unitSize"),
                ("totalunits", "totalUnits"),
                ("total_units", "totalUnits"),
                ("stdrrp", "stdRrp"),
                ("std_rrp", "stdRrp"),
                ("vatcode", "vatCode"),
                ("vat_code", "vatCode"),
                ("vatrate", "vatRate"),
                ("vat_rate", "vatRate"),
                ("vatamount", "vatAmount"),
                ("vat_amount", "vatAmount"),
                ("linegross", "lineGross"),
                ("line_gross", "lineGross"),
                ("linen et", "lineNet"),
                ("line_net", "lineNet"),
                ("unitprice", "unitPrice"),
                ("unit_price", "unitPrice"),
            ]:
                if k_src in it and k_dst not in it:
                    it[k_dst] = it.pop(k_src)
        except Exception:
            pass

        # --- Fix DWG Auto description/code when OCR returns combined tokens ---
        try:
            desc0 = str(it.get("description") or "")
            if desc0:
                d = desc0.lstrip("~").strip()

                # If lineNo is duplicated at the start of description, remove it
                ln = it.get("lineNo")
                if ln is not None:
                    try:
                        ln_i = int(float(ln))
                        d = re.sub(rf"^\s*{ln_i}\s+", "", d)
                    except Exception:
                        pass

                # If description starts with a code token (after removing lineNo), pull it into code if code missing
                if not str(it.get("code") or "").strip():
                    mcode = re.match(r"^([A-Za-z0-9][A-Za-z0-9\-]{1,20})\s+(.+)$", d)
                    if mcode:
                        tok = mcode.group(1)
                        # Avoid taking a pack token like 42X200ML as the code
                        if not re.match(r"^\d{1,3}\s*[xX]\s*\d", tok):
                            it["code"] = tok
                            d = mcode.group(2).strip()

                # If description now begins with a pack token, strip & map into fields
                mpack0 = re.match(r"^(\d{1,3})\s*[xX]\s*([0-9]+(?:\.[0-9]+)?)\s*(cl|ml|l|g|kg|oz)\b\s*(.*)$", d, flags=re.IGNORECASE)
                if mpack0:
                    cp_now = _safe_float(it.get("casePack")) or 0.0
                    if cp_now <= 0:
                        it["casePack"] = int(mpack0.group(1))
                    if not (it.get("unitSize") and str(it.get("unitSize")).strip()):
                        it["unitSize"] = f"{mpack0.group(2)}{mpack0.group(3).lower()}"
                    d = (mpack0.group(4) or "").strip()

                it["description"] = re.sub(r"\s{2,}", " ", d).strip()
        except Exception:
            pass


        # Normalise code when Cloud/Local returns prefixed tokens (e.g. "1 WAG3" or "~ 1 REDB-ED22")
        try:
            c = str(it.get("code") or "").strip()
            c = c.lstrip("~").strip()
            mmc = re.match(r"^(\d+)\s+([A-Za-z0-9\-]+)$", c)
            if mmc:
                # Keep qty as-is unless missing; strip the numeric prefix from code
                if not it.get("qty"):
                    try:
                        it["qty"] = float(mmc.group(1))
                    except Exception:
                        pass
                it["code"] = mmc.group(2)
            else:
                # If code empty but description starts with an alnum code token, pick it up
                if not c:
                    d0 = str(it.get("description") or "").strip()
                    d0 = d0.lstrip("~").strip()
                    mmc2 = re.match(r"^([A-Za-z0-9\-]{3,12})\s+(.+)$", d0)
                    if mmc2:
                        it["code"] = mmc2.group(1)
                        it["description"] = mmc2.group(2)
        except Exception:
            pass

        # DWG: extract PM/PMP RRP from description into stdRrp (when missing)
        try:
            if (_safe_float(it.get("stdRrp")) or 0.0) <= 0.0:
                rr, dd = _extract_std_rrp_from_desc(str(it.get("description") or ""))
                if rr and rr > 0:
                    it["stdRrp"] = float(rr)
                    if dd:
                        it["description"] = dd
        except Exception:
            pass

        # Sometimes DWG puts VAT code in vatRate field (e.g. vatRate='S/A')
        vr_raw = it.get("vatRate")
        vc_raw = it.get("vatCode")
        if (not vc_raw) and isinstance(vr_raw, str):
            vr_str = vr_raw.strip()
            if vr_str and len(vr_str) <= 8 and re.search(r"[A-Za-z/]", vr_str):
                it["vatCode"] = vr_str
                vr_raw = None

        if not it.get("vatCode"):
            it["vatCode"] = default_code

        # VAT rate numeric
        rate_val = _as_float_or_none(vr_raw)
        if rate_val is None:
            vc = it.get("vatCode") or ""
            if isinstance(vc, str) and re.search(r"\bS/A\b", vc, flags=re.I):
                rate_val = 20.0
            else:
                rate_val = default_rate
        it["vatRate"] = rate_val if rate_val is not None else 0.0

        # Total units (qty * casePack)
        qty = _safe_float(it.get("qty")) or 0.0
        case_pack = _safe_float(it.get("casePack"))
        # Treat blanks/zero as missing (Cloud sometimes returns 0/0.0)
        if case_pack is None or case_pack <= 0:
            desc = str(it.get("description") or "")
            # Patterns like: 6X20CL, 24x330ml, 12 X 1L, 10x50g
            m = re.search(r"\b(\d{1,3})\s*[xX]\s*([0-9]+(?:\.[0-9]+)?)\s*(cl|ml|l|g|kg|oz)\b", desc, flags=re.IGNORECASE)
            if m:
                case_pack = _safe_float(m.group(1))
                if not (it.get("unitSize") and str(it.get("unitSize")).strip()):
                    it["unitSize"] = f"{m.group(2)}{m.group(3).lower()}"
            else:
                # Fallback: just get the pack count if we can (e.g. 6x something)
                m2 = re.search(r"\b(\d{1,3})\s*[xX]\s*(?=\d)", desc)
                if m2:
                    case_pack = _safe_float(m2.group(1))
            if case_pack is not None and (not it.get("casePack") or str(it.get("casePack")).strip() in ("", "None", "0", "0.0")):
                it["casePack"] = int(case_pack) if abs(case_pack - int(case_pack)) < 1e-9 else case_pack

        if (it.get("totalUnits") in (None, "", 0)) and qty and case_pack:
            tu = qty * case_pack
            it["totalUnits"] = int(tu) if abs(tu - int(tu)) < 1e-9 else tu

        # Ensure lineNet (Gemini sometimes returns 0/None; recompute from unitPrice*qty)
        line_net = _safe_float(it.get("lineNet"))
        unit_price = _safe_float(it.get("unitPrice")) or 0.0
        if (line_net is None) or (line_net == 0 and unit_price > 0 and (qty or 0) > 0):
            if qty:
                line_net = unit_price * qty
            else:
                line_net = unit_price
        it["lineNet"] = round(line_net, 2) if line_net is not None else None

        # VAT amount
        vat_amt = _as_float_or_none(it.get("vatAmount"))
        if (vat_amt is None) or (vat_amt == 0 and (rate_val or 0.0) > 0 and (line_net or 0.0) > 0):
            vat_amt = round((line_net or 0.0) * (rate_val or 0.0) / 100.0, 2)
            it["vatAmount"] = vat_amt

        # Gross
        gross = _as_float_or_none(it.get("lineGross"))
        if (gross is None) or (gross == 0 and (line_net or 0.0) > 0):
            gross = round((line_net or 0.0) + (vat_amt or 0.0), 2)
            it["lineGross"] = gross


        # Compute POR (%) if missing/zero and we have stdRrp + units.
        # POR definition aligned with Booker: (SellingNet - UnitCost) / SellingNet * 100
        try:
            por_val = _as_float_or_none(it.get("por"))
            if por_val is None or por_val == 0:
                rrp_gross = _as_float_or_none(it.get("stdRrp")) or 0.0
                tu = _safe_float(it.get("totalUnits")) or 0.0
                if rrp_gross > 0 and tu > 0:
                    vat_rate = _as_float_or_none(it.get("vatRate")) or 0.0
                    selling_net = rrp_gross / (1.0 + (vat_rate / 100.0)) if vat_rate > 0 else rrp_gross
                    unit_cost = (line_net or 0.0) / tu if tu else 0.0
                    if selling_net > 0:
                        por_calc = ((selling_net - unit_cost) / selling_net) * 100.0
                        if por_calc < 0:
                            por_calc = 0.0
                        it["por"] = round(por_calc, 1)
        except Exception:
            pass

    return header, items

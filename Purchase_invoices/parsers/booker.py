from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
import re


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _fix_pm_code(text: str) -> str:
    """Repair common OCR drift in Booker PM/PMP description tokens."""
    s = str(text or "")
    if not s:
        return s
    s = re.sub(r"\b(PM)[LIL\|]([0-9]{2,4})\b", r"\g<1>1\2", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(PM)\s*[LIL\|]\s*([0-9]{2,4})\b", r"\g<1>1\2", s, flags=re.IGNORECASE)
    s = re.sub(r"(PM)[LIL\|]([0-9]{2,4})", r"\g<1>1\2", s, flags=re.IGNORECASE)
    s = re.sub(r"(PM)\s*[LIL\|]\s*([0-9]{2,4})", r"\g<1>1\2", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(PM)O([0-9]{2,4})\b", r"\g<1>0\2", s, flags=re.IGNORECASE)
    s = re.sub(r"(PM)O([0-9]{2,4})", r"\g<1>0\2", s, flags=re.IGNORECASE)
    return s


def _split_items_with_embedded_product_codes(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Split Booker rows where OCR merged a second product code into description."""
    out: List[Dict[str, Any]] = []
    code_re = re.compile(r"\b(\d{5,7})\b")
    for item in items or []:
        if not isinstance(item, dict):
            continue
        out.append(item)
        desc = str(item.get("description") or "").strip()
        matches = list(code_re.finditer(desc))
        split_at = None
        for m in matches:
            before = desc[:m.start()].strip()
            after = desc[m.end():].strip()
            if before and after:
                split_at = m.start()
                break
        if split_at is None:
            continue
        item["description"] = desc[:split_at].strip()
        tail = desc[split_at:].strip()
        tail_matches = list(code_re.finditer(tail))
        for idx, m in enumerate(tail_matches):
            code = m.group(1)
            seg_start = m.end()
            seg_end = tail_matches[idx + 1].start() if idx + 1 < len(tail_matches) else len(tail)
            seg_desc = tail[seg_start:seg_end].strip()
            if not seg_desc:
                continue
            clone = dict(item)
            clone["code"] = code
            clone["description"] = seg_desc
            clone["_splitFromEmbeddedCode"] = True
            out.append(clone)
    for idx, item in enumerate(out, start=1):
        try:
            item["lineNo"] = idx
        except Exception:
            pass
    return out


class BookerCombinedRowParser:
    """Booker line parser (parity + robustness).

    Designed to match the proven behaviour from v1.02.048 while adding:
    - VAT code mapping (B=20%, Z/A=0%, A=0%, R=5% by default; overridable via 'VAT mapping JSON' if present in text)
    - Optional FLAG token between unitPrice and value (P / SL / OCR variants)
    - Extract RRP and POR% when present
    - Computes totalUnits = casePack * qty, vatAmount and lineGross
    """

    DEFAULT_VAT_MAP = {
        "B": 20.0,
        "A": 0.0,
        "Z": 0.0,
        "Z/A": 0.0,
        "ZA": 0.0,
        "R": 5.0,
        "S/A": 20.0,
        "SA": 20.0,
    }

    @staticmethod
    def _normalise_line(line: str) -> str:
        s = (line or "").replace("\t", " ").replace("£", " ").strip()
        # Fix split decimals: '111. 41' -> '111.41'
        s = re.sub(r"(\d)\s*\.\s*(\d{2})", r"\1.\2", s)
        # Common OCR swaps for size tokens like 500m1; do not rewrite PM115/PMP179 codes.
        s = re.sub(r"(?<=\d)m1\b", "ml", s, flags=re.IGNORECASE)
        # Normalise spaces
        s = re.sub(r"\s+", " ", s)
        # Normalise common OCR variants of SL flag
        s = s.replace(" Sk ", " SL ").replace(" Sh ", " SL ").replace(" Si ", " SL ").replace(" S1 ", " SL ")
        return s.strip()

    @staticmethod
    def _is_code(tok: str) -> bool:
        t = re.sub(r"[^0-9]+", "", (tok or ""))
        return t.isdigit() and 5 <= len(t) <= 7

    @staticmethod
    def _as_float(tok: str):
        if tok is None:
            return None
        t = str(tok).strip()
        if not t:
            return None
        # Common OCR confusion for QTY = 1 (e.g. "L" / "I")
        if len(t) == 1:
            u = t.upper()
            if u in ("L", "I", "|"):
                return 1.0
            if u == "O":
                return 0.0
        t = t.replace(",", "").replace("%", "")
        if t.endswith("-") and len(t) > 1:
            t = "-" + t[:-1]
        t = re.sub(r"[^0-9.\-]+", "", t)
        if t in ("", "-", ".", "-."):
            return None
        try:
            return float(t)
        except Exception:
            return None

    @staticmethod
    def _as_float_strict(tok: str):
        """Parse numeric-only tokens, rejecting alphanumeric pack/size text."""
        if tok is None:
            return None
        t = str(tok).strip()
        if not t or re.search(r"[A-Za-z]", t):
            return None
        t = t.replace(",", "").replace("%", "")
        if t.endswith("-") and len(t) > 1:
            core = t[:-1]
            if re.fullmatch(r"\d+(?:\.\d+)?", core):
                try:
                    return -float(core)
                except Exception:
                    return None
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", t):
            return None
        try:
            return float(t)
        except Exception:
            return None

    @staticmethod
    def _norm_vat_code(tok: str) -> str:
        t = (tok or "").strip().upper().replace(" ", "")
        if t in ("ZA", "Z-A", "Z/A"):
            return "Z/A"
        return t

    @staticmethod
    def _looks_like_vat_code(tok: str) -> bool:
        t = BookerCombinedRowParser._norm_vat_code(tok)
        return bool(re.match(r"^(B|A|Z|Z/A|R)$", t))

    @staticmethod
    def _norm_flag(tok: str) -> str:
        t = (tok or "").strip().upper()
        if t in ("SH", "SK", "SI", "S1", "S/L"):
            return "SL"
        if t in ("SL", "P"):
            return t
        return ""

    @staticmethod
    def _extract_vat_map(text: str) -> Dict[str, float]:
        upper = (text or "").upper()
        upper = re.sub(r"(\d)\s*\.\s*(\d{2})", r"\1.\2", upper)
        vat_map = dict(BookerCombinedRowParser.DEFAULT_VAT_MAP)
        # Optional pattern: lines like "B: 20.0"
        for m in re.finditer(r"^\s*([A-Z](?:/[A-Z])?)\s*:\s*(\d{1,2}(?:\.\d+)?)\b", upper, flags=re.MULTILINE):
            code = BookerCombinedRowParser._norm_vat_code(m.group(1))
            rate = BookerCombinedRowParser._as_float(m.group(2))
            if rate is not None:
                vat_map[code] = float(rate)
        return vat_map

    def parse(self, text: str) -> List[Dict[str, Any]]:
        txt = (text or "")
        vat_map = self._extract_vat_map(txt)
        lines = [self._normalise_line(ln) for ln in txt.splitlines() if (ln or "").strip()]
        items: List[Dict[str, Any]] = []
        last_vat_code: Optional[str] = None
        promo_re = re.compile(
            r"\bBUY\s*ANY\b|\bBUY\s*\d+\b|\bBUY\d+\b|\bMULTI\b|\bPROMO\b|"
            r"\bPROMOTION\b|\bDEAL\b|\bOFFER\b|\bSAVE\b|\bSAVING\b|"
            r"\bDISC(?:OUNT)?\b|\bGET\s*\d+\s*FREE\b",
            re.IGNORECASE,
        )

        def _promo_amount(ln: str) -> Optional[float]:
            if not ln or not promo_re.search(ln):
                return None
            for tok in re.findall(r"-?\d+\.\d{2}-?", ln.replace(",", "")):
                try:
                    val = -float(tok[:-1]) if tok.endswith("-") and len(tok) > 1 else float(tok)
                except Exception:
                    continue
                if val < 0:
                    return val
            return None

        def _append_promo_line(ln: str, amount: float) -> None:
            vat_code = last_vat_code or "B"
            vat_rate = float(vat_map.get(vat_code, BookerCombinedRowParser.DEFAULT_VAT_MAP.get(vat_code, 0.0)))
            vat_amount = round(float(amount) * (vat_rate / 100.0), 2)
            items.append({
                "lineNo": None,
                "code": "PROMO",
                "description": f"PROMO: {ln.strip()}",
                "casePack": 0,
                "unitSize": 0,
                "qty": 0.0,
                "totalUnits": 0.0,
                "unitPrice": 0.0,
                "flag": "",
                "lineNet": float(amount),
                "vatCode": vat_code,
                "vatRate": float(vat_rate),
                "vatAmount": float(vat_amount),
                "lineGross": float(round(float(amount) + vat_amount, 2)),
                "stdRrp": 0.0,
                "por": 0.0,
                "isVoid": False,
                "rowTag": "PROMO",
            })

        def should_skip(ln_up: str) -> bool:
            if not ln_up:
                return True
            # Booker headers / totals / noise
            if any(k in ln_up for k in ("SUB-TOTAL", "TOTAL ITEMS", "TOTAL SAVINGS", "SAVING DETAILS", "RATE ", "INVOICE TOTAL", "TOTALS")):
                return True
            if ln_up.startswith("CODE ") or "CODE DESCRIPTION" in ln_up:
                return True
            return False

        for ln in lines:
            up = ln.upper()
            if should_skip(up):
                continue

            toks = ln.split()
            if len(toks) < 7:
                promo_val = _promo_amount(ln)
                if promo_val is None and promo_re.search(up) and "FREE" in up:
                    promo_val = 0.0
                if promo_val is not None and (not toks or not self._is_code(toks[0])):
                    _append_promo_line(ln, promo_val)
                continue
            if not self._is_code(toks[0]):
                promo_val = _promo_amount(ln)
                if promo_val is None and promo_re.search(up) and "FREE" in up:
                    promo_val = 0.0
                if promo_val is not None:
                    _append_promo_line(ln, promo_val)
                continue

            code_val = re.sub(r"[^0-9]+", "", toks[0])

            # Find VAT code index scanning from right. VAT code is followed by RRP and POR%, but allow flexible positions.
            vat_idx = None
            for i in range(len(toks) - 1, 0, -1):
                if self._looks_like_vat_code(toks[i]):
                    # ensure there is at least one numeric token after VAT (RRP) or before VAT (value)
                    if any(self._as_float(x) is not None for x in toks[i+1:i+3]) or any(self._as_float(x) is not None for x in toks[max(0,i-3):i]):
                        vat_idx = i
                        break
            if vat_idx is None:
                continue

            vat_code = self._norm_vat_code(toks[vat_idx])
            vat_rate = float(vat_map.get(vat_code, BookerCombinedRowParser.DEFAULT_VAT_MAP.get(vat_code, 0.0)))

            # POR% (usually last token)
            por = None
            for j in range(len(toks) - 1, vat_idx, -1):
                if "%" in toks[j]:
                    por = self._as_float(toks[j])
                    break
            # RRP (usually immediately after VAT code)
            std_rrp = None
            if vat_idx + 1 < len(toks):
                std_rrp = self._as_float(toks[vat_idx + 1])

            # VALUE = last numeric token before VAT code
            value_idx = None
            for j in range(vat_idx - 1, 1, -1):
                if self._as_float(toks[j]) is not None:
                    value_idx = j
                    break
            if value_idx is None:
                continue
            line_net = float(self._as_float(toks[value_idx]) or 0.0)

            # FLAG between unitPrice and VALUE (optional)
            flag = ""
            scan_left_from = value_idx - 1
            if scan_left_from >= 0:
                f = self._norm_flag(toks[scan_left_from])
                if f:
                    flag = f
                    scan_left_from = value_idx - 2

            # UNIT PRICE = nearest numeric to the left of VALUE (skipping FLAG if present)
            unit_price = None
            unit_price_idx = None
            for j in range(scan_left_from, 1, -1):
                v = self._as_float(toks[j])
                if v is not None:
                    unit_price = float(v)
                    unit_price_idx = j
                    break
            if unit_price_idx is None:
                continue

            # QTY = nearest numeric to the left of unit price
            qty = None
            qty_idx = None
            for j in range(unit_price_idx - 1, 1, -1):
                v = self._as_float(toks[j])
                if v is not None:
                    qty = float(v)
                    qty_idx = j
                    break
            if qty_idx is None:
                continue

            # PACK + UNIT SIZE immediately before qty
            if qty_idx - 2 < 1:
                continue
            case_pack = self._as_float(toks[qty_idx - 2])
            unit_size = toks[qty_idx - 1]
            if case_pack is None:
                continue
            case_pack = int(case_pack)

            # DESCRIPTION is everything between code and pack token
            desc_tokens = toks[1:qty_idx - 2]
            if not desc_tokens:
                continue
            description = " ".join(desc_tokens).strip()

            # Derived
            total_units = float(case_pack) * float(qty or 0.0)
            # If VALUE missing/zero, compute from qty * unitPrice
            if (line_net <= 0.0) and (qty is not None) and (unit_price is not None):
                line_net = round(float(qty) * float(unit_price), 2)

            # Handle void/refund lines (e.g., VOID NOTE) - keep negatives consistent
            is_void = False
            try:
                qv = float(qty or 0.0)
                ln = float(line_net or 0.0)
            except Exception:
                qv = 0.0
                ln = 0.0
            if qv < 0 or ln < 0:
                is_void = True
            # If qty negative but value positive, flip value negative
            if qv < 0 and ln > 0:
                line_net = -abs(ln)
                ln = float(line_net)
            # If value negative but qty positive, flip qty negative
            if ln < 0 and qv > 0:
                qty = -abs(qv)
                total_units = float(case_pack) * float(qty or 0.0)
            # Unit price should remain positive for display/export
            if unit_price is not None:
                try:
                    if float(unit_price) < 0:
                        unit_price = abs(float(unit_price))
                except Exception:
                    pass

            vat_amount = round(line_net * (vat_rate / 100.0), 2)
            line_gross = round(line_net + vat_amount, 2)
            # NOTE: duplicates are allowed (same code/value may appear multiple times on a Booker invoice)

            items.append({
                "lineNo": None,
                "code": code_val,
                "description": description,
                "casePack": int(case_pack),
                "unitSize": unit_size,
                "qty": float(qty or 0.0),
                "totalUnits": float(total_units),
                "unitPrice": float(unit_price or 0.0),
                "flag": flag,
                "lineNet": float(line_net),
                "vatCode": vat_code,
                "vatRate": float(vat_rate),
                "vatAmount": float(vat_amount),
                "lineGross": float(line_gross),
                "stdRrp": float(std_rrp) if std_rrp is not None else 0.0,
                "por": float(por) if por is not None else 0.0,
                "isVoid": is_void,
            })
            last_vat_code = vat_code

        return _split_items_with_embedded_product_codes(items)

    def parse_split_layout(self, text: str) -> List[Dict[str, Any]]:
        """Handle Booker invoices where the table is split across blocks (seen in some scanned PDFs).

        Pattern example (OCR):
        - Block 1: CODE DESCRIPTION PACK SIZE (no qty/price/value)
        - Block 2: QTY PRICE lines
        - Block 3: VALUE VAT RRP POR lines

        We extract each block as ordered lists and merge by index.
        """
        txt = (text or "")
        txt_fix = re.sub(r"(\d)\s*\.\s*(\d{2})", r"\1.\2", txt)
        vat_map = self._extract_vat_map(txt_fix)
        lines = [self._normalise_line(ln) for ln in txt_fix.splitlines() if (ln or "").strip()]
        up_all = "\n".join(lines).upper()

        m_total = re.search(r"TOTAL\s+ITEMS\s*:\s*(\d+)", up_all, flags=re.I)
        expected_total = int(m_total.group(1)) if m_total else 0

        # 1) Code/desc/pack/size block
        code_rows: List[Tuple[str, str, int, str]] = []
        in_code = False
        for ln in lines:
            up = ln.upper()
            if ("CODE" in up and "DESCRIPTION" in up and "PACK" in up and "SIZE" in up and "QTY" not in up):
                in_code = True
                continue
            if in_code:
                if "TOTAL ITEMS" in up:
                    break
                toks = ln.split()
                if not toks:
                    continue
                if not self._is_code(toks[0]):
                    continue

                # Find last integer-like numeric as casePack, followed by unitSize
                pack_idx = None
                for i in range(len(toks) - 2, 0, -1):
                    v = self._as_float(toks[i])
                    if v is not None and abs(v - round(v)) < 1e-6:
                        pack_idx = i
                        break
                if pack_idx is None or pack_idx + 1 >= len(toks):
                    continue

                case_pack = int(self._as_float(toks[pack_idx]) or 0)
                unit_size = toks[pack_idx + 1]
                desc = " ".join(toks[1:pack_idx]).strip()
                code_val = re.sub(r"[^0-9]+", "", toks[0])

                if case_pack <= 0 or not desc:
                    continue

                code_rows.append((code_val, desc, case_pack, unit_size))

                if expected_total and len(code_rows) >= expected_total:
                    # Sometimes the code block is complete and we can stop early
                    break

        # If we didn't detect the split layout, abort
        if not code_rows:
            return []

        # 2) Qty/Price block (usually after a header line containing both QTY and PRICE)
        qty_price: List[Tuple[float, float]] = []
        in_qp = False
        for ln in lines:
            up = ln.upper()
            if ("QTY" in up and "PRICE" in up):
                in_qp = True
                continue
            if not in_qp:
                continue
            # Stop when we reach the next block
            if ("VALUE" in up and "VAT" in up) or up.startswith("STD") or ("VALUE" in up and "RRP" in up):
                break

            # Extract numbers from the line
            nums = re.findall(r"-?\d+(?:\.\d+)?", ln.replace(",", ""))
            if not nums:
                continue
            if len(nums) >= 2:
                qty = float(nums[0])
                price = float(nums[1])
            else:
                qty = 1.0
                price = float(nums[0])
            qty_price.append((qty, price))

            if expected_total and len(qty_price) >= expected_total:
                break

        # 3) Value/VAT/RRP/POR block
        value_rows: List[Tuple[float, str, float, float]] = []
        in_v = False
        for ln in lines:
            up = ln.upper()
            if ("VALUE" in up and "VAT" in up and "RRP" in up):
                in_v = True
                continue
            if not in_v:
                continue

            if any(k in up for k in ("TOTALS", "INVOICE TOTAL", "PLEASE DO NOT", "RATE ", "NETT")):
                # allow some noise, but if we already got enough rows we can stop
                if expected_total and len(value_rows) >= expected_total:
                    break
                continue

            toks = ln.split()
            if len(toks) < 2:
                continue

            value = self._as_float(toks[0])
            if value is None:
                continue

            vat_code = ""
            std_rrp = 0.0
            por = 0.0

            # Find VAT code token
            for t in toks[1:]:
                if self._looks_like_vat_code(t):
                    vat_code = self._norm_vat_code(t)
                    break
            if not vat_code:
                continue

            # RRP: first numeric after VAT code
            passed_vat = False
            for t in toks[1:]:
                if not passed_vat:
                    if self._looks_like_vat_code(t):
                        passed_vat = True
                    continue
                if std_rrp == 0.0:
                    v = self._as_float(t)
                    if v is not None:
                        std_rrp = float(v)
                        continue
                if "%" in t and por == 0.0:
                    v = self._as_float(t)
                    por = float(v or 0.0)

            value_rows.append((float(value), vat_code, float(std_rrp or 0.0), float(por or 0.0)))

            if expected_total and len(value_rows) >= expected_total:
                break

        # Merge by index
        n = len(code_rows)
        if expected_total:
            n = min(n, expected_total)
        if qty_price:
            n = min(n, len(qty_price))
        if value_rows:
            n = min(n, len(value_rows))

        if n <= 0:
            return []

        items: List[Dict[str, Any]] = []
        for i in range(n):
            code_val, desc, case_pack, unit_size = code_rows[i]
            qty, unit_price = qty_price[i] if i < len(qty_price) else (1.0, 0.0)
            line_net, vat_code, std_rrp, por = value_rows[i] if i < len(value_rows) else (round(qty * unit_price, 2), "B", 0.0, 0.0)

            vat_rate = float(vat_map.get(vat_code, BookerCombinedRowParser.DEFAULT_VAT_MAP.get(vat_code, 0.0)))
            # Handle void/refund lines (e.g., VOID NOTE) - keep negatives consistent
            is_void = False
            try:
                qv = float(qty or 0.0)
                ln = float(line_net or 0.0)
            except Exception:
                qv = 0.0
                ln = 0.0
            if qv < 0 or ln < 0:
                is_void = True
            # If qty negative but value positive, flip value negative
            if qv < 0 and ln > 0:
                line_net = -abs(ln)
                ln = float(line_net)
            # If value negative but qty positive, flip qty negative
            if ln < 0 and qv > 0:
                qty = -abs(qv)
                total_units = float(case_pack) * float(qty or 0.0)
            # Unit price should remain positive for display/export
            if unit_price is not None:
                try:
                    if float(unit_price) < 0:
                        unit_price = abs(float(unit_price))
                except Exception:
                    pass

            vat_amount = round(float(line_net) * (vat_rate / 100.0), 2)
            line_gross = round(float(line_net) + float(vat_amount), 2)
            total_units = float(case_pack) * float(qty)

            items.append({
                "lineNo": None,
                "code": code_val,
                "description": desc,
                "casePack": int(case_pack),
                "unitSize": unit_size,
                "qty": float(qty),
                "totalUnits": float(total_units),
                "unitPrice": float(unit_price),
                "flag": "",
                "lineNet": float(line_net),
                "vatCode": vat_code,
                "vatRate": float(vat_rate),
                "vatAmount": float(vat_amount),
                "lineGross": float(line_gross),
                "stdRrp": float(std_rrp),
                "por": float(por),
                "isVoid": is_void,
            })

        return items

    def build_rowified_text(self, text: str) -> str:
        """Rebuild stacked Booker OCR into row-like lines the main parser can consume."""
        txt = (text or "")
        txt_fix = re.sub(r"(\d)\s*\.\s*(\d{2})", r"\1.\2", txt)
        lines = [self._normalise_line(ln) for ln in txt_fix.splitlines() if (ln or "").strip()]
        up_all = "\n".join(lines).upper()

        m_total = re.search(r"TOTAL\s+ITEMS\s*:\s*(\d+)", up_all, flags=re.I)
        expected_total = int(m_total.group(1)) if m_total else 0

        code_rows: List[Tuple[str, str, int, str]] = []
        for i_ln, ln in enumerate(lines):
            toks = ln.split()
            if not toks or not self._is_code(toks[0]):
                continue
            code_val = re.sub(r"[^0-9]+", "", toks[0])
            desc = _fix_pm_code(" ".join(toks[1:]).strip())
            case_pack = 0
            unit_size = ""
            for j in range(i_ln + 1, min(i_ln + 3, len(lines))):
                nxt = lines[j].split()
                if len(nxt) < 2:
                    continue
                v = self._as_float(nxt[0])
                if v is not None and abs(v - round(v)) < 1e-6 and re.search(r"[A-Za-z]", nxt[1]):
                    case_pack = int(v)
                    unit_size = nxt[1]
                    break
            if desc:
                code_rows.append((code_val, desc, case_pack, unit_size))
            if expected_total and len(code_rows) >= expected_total:
                break

        qty_price: List[Tuple[float, float]] = []
        in_qp = False
        pending_qty = None
        for ln in lines:
            up = ln.upper()
            if ("QTY" in up and "PRICE" in up) or up.strip() == "QTY":
                in_qp = True
                continue
            if not in_qp:
                continue
            if ("VALUE" in up and "VAT" in up) or up.startswith("STD") or ("VALUE" in up and "RRP" in up):
                break
            nums = re.findall(r"-?\d+(?:\.\d+)?", ln.replace(",", ""))
            if not nums:
                continue
            for n in nums:
                try:
                    v = float(n)
                except Exception:
                    continue
                if pending_qty is None:
                    pending_qty = v
                else:
                    qty_price.append((float(pending_qty), float(v)))
                    pending_qty = None
            if expected_total and len(qty_price) >= expected_total:
                break

        value_rows: List[Tuple[float, str, float, float]] = []
        in_v = False
        saw_std = False
        pending_value: Optional[float] = None
        pending_vat = ""
        pending_rrp = 0.0
        pending_por = 0.0
        for ln in lines:
            up = ln.upper()
            if up.strip() == "STD" or ("STD" in up and "RRP" in up):
                saw_std = True
                in_v = True
                continue
            if ("VALUE" in up and "VAT" in up and "RRP" in up) or up.strip() == "VALUE":
                if saw_std or not in_v:
                    in_v = True
                continue
            if not in_v:
                continue
            if any(k in up for k in ("TOTALS", "INVOICE TOTAL", "PLEASE DO NOT", "RATE ", "NETT")):
                if value_rows:
                    break
                continue
            if re.search(r"\b[A-Z]{1}/?A?:\s*\d+(?:\.\d+)?", up) and re.search(r"\d+\.\d+", up):
                continue
            toks = ln.split()
            if len(toks) == 1:
                tok = toks[0]
                if self._looks_like_vat_code(tok):
                    pending_vat = self._norm_vat_code(tok)
                elif "%" in tok:
                    v = self._as_float(tok)
                    if v is not None:
                        pending_por = float(v)
                else:
                    v = self._as_float(tok)
                    if v is not None:
                        if pending_value is None:
                            pending_value = float(v)
                        elif pending_rrp == 0.0:
                            pending_rrp = float(v)
                if pending_value is not None and pending_vat:
                    value_rows.append((float(pending_value), pending_vat, float(pending_rrp or 0.0), float(pending_por or 0.0)))
                    pending_value, pending_vat, pending_rrp, pending_por = None, "", 0.0, 0.0
                if expected_total and len(value_rows) >= expected_total:
                    break
                continue
            if len(toks) >= 2:
                value = self._as_float(toks[0])
                if value is None:
                    continue
                vat_code = ""
                std_rrp = 0.0
                por = 0.0
                for t in toks[1:]:
                    if self._looks_like_vat_code(t):
                        vat_code = self._norm_vat_code(t)
                        break
                if not vat_code:
                    continue
                passed_vat = False
                for t in toks[1:]:
                    if not passed_vat:
                        if self._looks_like_vat_code(t):
                            passed_vat = True
                        continue
                    if std_rrp == 0.0:
                        v = self._as_float(t)
                        if v is not None:
                            std_rrp = float(v)
                            continue
                    if "%" in t and por == 0.0:
                        v = self._as_float(t)
                        por = float(v or 0.0)
                value_rows.append((float(value), vat_code, float(std_rrp or 0.0), float(por or 0.0)))
                if expected_total and len(value_rows) >= expected_total:
                    break

        n = len(code_rows)
        if expected_total:
            n = min(n, expected_total)
        if qty_price:
            n = min(n, len(qty_price))
        if value_rows:
            n = min(n, len(value_rows))
        if n <= 0:
            return ""

        out_lines: List[str] = []
        for i in range(n):
            code_val, desc, case_pack, unit_size = code_rows[i]
            qty, unit_price = qty_price[i] if i < len(qty_price) else (1.0, 0.0)
            line_net, vat_code, std_rrp, por = value_rows[i] if i < len(value_rows) else (round(qty * unit_price, 2), "B", 0.0, 0.0)
            if (unit_price == 0.0) and (qty in (0.0, 1.0)) and line_net > 0:
                unit_price = float(line_net)
                if qty == 0.0:
                    qty = 1.0
            if unit_price > 0 and line_net > 0 and line_net < (unit_price * 0.75) and std_rrp >= unit_price:
                line_net, std_rrp = std_rrp, line_net
            out_lines.append(
                f"{code_val} {desc} {case_pack} {unit_size} {qty} {unit_price:.2f} {line_net:.2f} {vat_code} {std_rrp:.2f} {por:.1f}%"
            )
        return "\n".join(out_lines)

    def parse_stacked_layout(self, text: str) -> List[Dict[str, Any]]:
        """Handle Booker OCR where code/desc/qty/value are stacked on separate lines."""
        txt = (text or "")
        txt_fix = re.sub(r"(\d)\s*\.\s*(\d{2})", r"\1.\2", txt)
        vat_map = self._extract_vat_map(txt_fix)
        lines = [self._normalise_line(ln) for ln in txt_fix.splitlines() if (ln or "").strip()]

        def _is_header_line(s: str) -> bool:
            su = s.upper()
            if not su:
                return True
            if any(k in su for k in ("QTY PRICE", "VALUE VAT", "CODE DESCRIPTION", "PACK SIZE", "INVOICE", "TOTAL", "SUB-TOTAL", "PROMOTION", "SAVING", "RATE ")):
                return True
            return False

        def _unit_size_token(tok: str) -> bool:
            t = (tok or "").strip().upper()
            if re.match(r"^\d+(?:\.\d+)?(ML|L|LTR|LT|CL|G|KG|PK|S|ROLL|ROLLS)$", t):
                return True
            if re.match(r"^\d+[X*]\d+(?:\.\d+)?(ML|L|LTR|LT|CL|G|KG|PK|S|ROLL|ROLLS)$", t):
                return True
            return False

        def _extract_pack_line(block_lines: List[str]) -> Tuple[Optional[int], str]:
            for ln in block_lines:
                toks = ln.split()
                if len(toks) < 2:
                    continue
                for i in range(len(toks) - 1):
                    a = self._as_float(toks[i])
                    b = toks[i + 1]
                    if a is not None and abs(a - round(a)) < 1e-6 and _unit_size_token(b):
                        return int(a), b
            return None, ""

        def _extract_qty_price(block_lines: List[str]) -> Tuple[Optional[float], Optional[float], str]:
            for ln in block_lines:
                if _is_header_line(ln) or "%" in ln:
                    continue
                toks = ln.split()
                nums = [self._as_float_strict(t) for t in toks if self._as_float_strict(t) is not None]
                if len(nums) >= 2:
                    qty_cand = nums[0]
                    price_cand = nums[1]
                    if qty_cand is not None and abs(qty_cand - round(qty_cand)) < 1e-6:
                        flag = ""
                        for t in toks:
                            f = self._norm_flag(t)
                            if f:
                                flag = f
                                break
                        return float(qty_cand), float(price_cand), flag
            return None, None, ""

        def _extract_value_vat(block_lines: List[str]) -> Tuple[Optional[float], str, float, float]:
            vat_line_idx = None
            vat_code = ""
            vat_tok_idx = None
            for i, ln in enumerate(block_lines):
                toks = ln.split()
                if not toks:
                    continue
                for j, t in enumerate(toks):
                    if self._looks_like_vat_code(t):
                        vat_code = self._norm_vat_code(t)
                        vat_line_idx = i
                        vat_tok_idx = j
                        break
                if vat_line_idx is not None:
                    break
            if vat_line_idx is None:
                return None, "", 0.0, 0.0

            toks = block_lines[vat_line_idx].split()
            value = None
            if vat_tok_idx is not None:
                for t in reversed(toks[:vat_tok_idx]):
                    v = self._as_float(t)
                    if v is not None:
                        value = float(v)
                        break
            if value is None:
                for t in toks:
                    v = self._as_float(t)
                    if v is not None:
                        value = float(v)
                        break
            if value is None:
                return None, "", 0.0, 0.0

            std_rrp = 0.0
            por = 0.0
            passed_vat = False
            for t in toks:
                if not passed_vat:
                    if self._looks_like_vat_code(t):
                        passed_vat = True
                    continue
                if std_rrp == 0.0:
                    v = self._as_float(t)
                    if v is not None:
                        std_rrp = float(v)
                        continue
                if "%" in t and por == 0.0:
                    v = self._as_float(t)
                    por = float(v or 0.0)

            if (std_rrp == 0.0 or por == 0.0) and vat_line_idx is not None:
                for ln in block_lines[vat_line_idx + 1:]:
                    if _is_header_line(ln):
                        break
                    if "%" not in ln and std_rrp > 0.0:
                        continue
                    toks2 = ln.split()
                    if not toks2:
                        continue
                    if std_rrp == 0.0:
                        v0 = self._as_float(toks2[0])
                        if v0 is not None:
                            std_rrp = float(v0)
                    if por == 0.0:
                        for t in toks2:
                            if "%" in t:
                                v = self._as_float(t)
                                por = float(v or 0.0)
                                break
                    if std_rrp > 0.0 or por > 0.0:
                        break

            return value, vat_code, std_rrp, por

        items: List[Dict[str, Any]] = []
        code_idxs = []
        for i, ln in enumerate(lines):
            toks = ln.split()
            if not toks or not self._is_code(toks[0]):
                continue
            try:
                has_desc_word = any(re.search(r"[A-Za-z]{2,}", t) and not self._looks_like_vat_code(t) for t in toks[1:])
                looks_like_value = any(self._looks_like_vat_code(t) for t in toks[1:]) and sum(
                    1 for t in toks[1:] if self._as_float(t) is not None
                ) >= 1 and not has_desc_word
                if has_desc_word and not looks_like_value:
                    code_idxs.append(i)
            except Exception:
                code_idxs.append(i)

        for idx, start in enumerate(code_idxs):
            end = code_idxs[idx + 1] if idx + 1 < len(code_idxs) else len(lines)
            block = lines[start:end]
            if not block:
                continue
            first = block[0].split()
            code_val = re.sub(r"[^0-9]+", "", first[0])
            desc_parts = []
            if len(first) > 1:
                desc_parts.extend(first[1:])

            for ln in block[1:]:
                if _is_header_line(ln):
                    continue
                if re.fullmatch(r"\d{5,7}", ln.strip()):
                    continue
                if any(self._looks_like_vat_code(t) for t in ln.split()):
                    continue
                try:
                    toks_ln = ln.split()
                    has_desc = any(re.search(r"[A-Za-z]{2,}", t) and not self._looks_like_vat_code(t) for t in toks_ln)
                    looks_value = any(self._looks_like_vat_code(t) for t in toks_ln) and sum(
                        1 for t in toks_ln if self._as_float(t) is not None
                    ) >= 1 and not has_desc
                    if looks_value:
                        continue
                except Exception:
                    pass
                nums = [self._as_float(t) for t in ln.split() if self._as_float(t) is not None]
                if len(nums) >= 2:
                    break
                desc_parts.append(ln)

            case_pack, unit_size = _extract_pack_line(block)
            qty, unit_price, flag = _extract_qty_price(block)
            line_net, vat_code, std_rrp, por = _extract_value_vat(block)

            if not code_val or qty is None or unit_price is None or line_net is None or not vat_code:
                continue
            if case_pack is None:
                case_pack = 0
            description = _collapse_spaces(" ".join(desc_parts)).strip() or " ".join([t for t in first[1:] if t])

            vat_rate = float(vat_map.get(vat_code, BookerCombinedRowParser.DEFAULT_VAT_MAP.get(vat_code, 0.0)))
            total_units = float(case_pack or 0) * float(qty or 0.0)
            if (line_net <= 0.0) and (qty is not None) and (unit_price is not None):
                line_net = round(float(qty) * float(unit_price), 2)
            vat_amount = round(float(line_net) * (vat_rate / 100.0), 2)
            line_gross = round(float(line_net) + float(vat_amount), 2)

            items.append({
                "lineNo": None,
                "code": code_val,
                "description": description,
                "casePack": int(case_pack or 0),
                "unitSize": unit_size,
                "qty": float(qty or 0.0),
                "totalUnits": float(total_units),
                "unitPrice": float(unit_price or 0.0),
                "flag": flag,
                "lineNet": float(line_net),
                "vatCode": vat_code,
                "vatRate": float(vat_rate),
                "vatAmount": float(vat_amount),
                "lineGross": float(line_gross),
                "stdRrp": float(std_rrp or 0.0),
                "por": float(por or 0.0),
                "isVoid": True if (qty or 0) < 0 or (line_net or 0) < 0 else False,
            })

        return items

    def parse_stacked_ocr_text(self, text: str) -> List[Dict[str, Any]]:
        """Parse Booker OCR text where each row is emitted as a vertical stack of lines."""
        txt = (text or "")
        txt_fix = re.sub(r"(\d)\s*\.\s*(\d{2})", r"\1.\2", txt)
        vat_map = self._extract_vat_map(txt_fix)
        lines = [self._normalise_line(ln) for ln in txt_fix.splitlines() if (ln or "").strip()]
        items: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        void_next = False

        def _is_code_line(s: str) -> bool:
            toks = s.split()
            return bool(toks and self._is_code(toks[0]) and len(toks) > 1)

        def _is_pack_line(s: str) -> bool:
            toks = s.split()
            if len(toks) < 2:
                return False
            v = self._as_float_strict(toks[0])
            if v is None or abs(v - round(v)) > 1e-6:
                return False
            return bool(re.search(r"[A-Za-z]", toks[1]))

        def _is_qty_line(s: str) -> bool:
            return bool(re.fullmatch(r"-?\d+(?:\.\d+)?-?\.?", s.strip()))

        def _is_moneyish(s: str) -> bool:
            return self._as_float(s) is not None

        def _skip_noise(idx: int) -> int:
            while idx < len(lines):
                up = lines[idx].upper().strip()
                if not up:
                    idx += 1
                    continue
                if up in {"STD", "CODE", "DESCRIPTION", "PACK SIZE", "QTY", "PRICE", "VALUE", "VAT", "RRP", "POR", "RRP POR"}:
                    idx += 1
                    continue
                if any(k in up for k in (
                    "SUB-TOTAL", "TOTAL ITEMS", "TOTAL SAVINGS", "PROMOTIONS SAVED YOU",
                    "CLUB PROMOTIONS SAVED YOU", "INVOICE TOTAL", "PLEASE DO NOT BRING BACK",
                    "TRAYS AND KEGS", "SAVING DETAILS", "GOODS", "EXC.VAT", "/CONT", "PAGE 0",
                    "BRANCH ", "CUSTOMER ", "DATE ", "TIME ", "RATE", "NETT", "MULT"
                )):
                    idx += 1
                    continue
                if up in {
                    "RETAIL GROCERY", "CHILLED", "CONFECTIONERY", "FROZEN FOOD",
                    "WINES SPIRITS BEERS", "FROZEN", "TOBACCO"
                }:
                    idx += 1
                    continue
                break
            return idx

        i = 0
        while i < len(lines):
            i = _skip_noise(i)
            if i >= len(lines):
                break
            line = lines[i].strip()
            up = line.upper()

            if up == "VOID NOTE":
                void_next = True
                i += 1
                continue

            if _is_code_line(line):
                toks = line.split()
                code_val = re.sub(r"[^0-9]+", "", toks[0])
                desc = _fix_pm_code(" ".join(toks[1:]).strip())
                pending.append({"code": code_val, "description": desc})
                i += 1
                continue

            if not pending or not _is_pack_line(line):
                i += 1
                continue

            current = pending.pop(0)
            pack_toks = line.split()
            case_pack = int(self._as_float(pack_toks[0]) or 0)
            unit_size = pack_toks[1]

            j = i + 1
            j = _skip_noise(j)
            if j >= len(lines) or not _is_qty_line(lines[j]):
                i += 1
                continue
            qty = float(self._as_float(lines[j]) or 0.0)

            j += 1
            j = _skip_noise(j)
            if j >= len(lines):
                break
            price_line = lines[j].strip()
            price_toks = price_line.split()
            unit_price = 0.0
            flag = ""
            for tok in price_toks:
                f = self._norm_flag(tok)
                if f:
                    flag = f
                    continue
                v = self._as_float(tok)
                if v is not None:
                    unit_price = float(v)
                    break
            if unit_price == 0.0 and not _is_moneyish(price_line):
                i += 1
                continue

            j += 1
            j = _skip_noise(j)
            if j >= len(lines):
                break
            value_line = lines[j].strip()
            value_toks = value_line.split()
            line_net = 0.0
            vat_code = ""
            for tok in value_toks:
                if not vat_code and self._looks_like_vat_code(tok):
                    vat_code = self._norm_vat_code(tok)
                    continue
                if self._as_float(tok) is not None and line_net == 0.0:
                    line_net = float(self._as_float(tok) or 0.0)
            if not vat_code:
                j += 1
                j = _skip_noise(j)
                if j < len(lines) and self._looks_like_vat_code(lines[j].strip()):
                    vat_code = self._norm_vat_code(lines[j].strip())
                else:
                    vat_code = "B"
                    j -= 1

            j += 1
            j = _skip_noise(j)
            std_rrp = 0.0
            por = 0.0
            if j < len(lines):
                rrp_line = lines[j].strip()
                if "%" in rrp_line:
                    for tok in rrp_line.split():
                        if std_rrp == 0.0 and self._as_float(tok) is not None:
                            std_rrp = float(self._as_float(tok) or 0.0)
                            continue
                        if "%" in tok:
                            por = float(self._as_float(tok) or 0.0)
                    j += 1

            vat_rate = float(vat_map.get(vat_code, BookerCombinedRowParser.DEFAULT_VAT_MAP.get(vat_code, 0.0)))
            if qty < 0 and line_net > 0:
                line_net = -abs(line_net)
            is_void = bool(void_next or qty < 0 or line_net < 0)
            void_next = False
            total_units = float(abs(qty) * case_pack)
            vat_amount = round(float(line_net) * (vat_rate / 100.0), 2)
            line_gross = round(float(line_net) + float(vat_amount), 2)

            items.append({
                "lineNo": None,
                "code": current["code"],
                "description": current["description"],
                "casePack": int(case_pack),
                "unitSize": unit_size,
                "qty": float(qty),
                "totalUnits": float(total_units),
                "unitPrice": float(unit_price),
                "flag": flag,
                "lineNet": float(line_net),
                "vatCode": vat_code,
                "vatRate": float(vat_rate),
                "vatAmount": float(vat_amount),
                "lineGross": float(line_gross),
                "stdRrp": float(std_rrp),
                "por": float(por),
                "isVoid": is_void,
            })
            i = j

        return items


# -------------------------
# Parfetts parsing (Purchase Invoice)
# -------------------------

PARFETTS_VAT_MAP_DEFAULT = {
    "A": 20.0,
    "Z": 0.0,
    "R": 5.0,
    "B": 20.0,
}

def _booker_extract_customer_name(extracted_text: str) -> str:
    """Best-effort Booker customer-name extraction from OCR/text layer.

    AUTO/Local parsing can miss customerName. This function:
    - Searches for 'Customer' anchors and captures inline/next-line values
    - Cleans common OCR separators (|) and removes trailing 'Customer'
    - Removes PO number suffixes and other obvious non-name fragments
    """
    t = extracted_text or ""
    if not t.strip():
        return ""

def _booker_autofill_customer_name(header: Dict[str, Any], extracted_text: str, template_hit: Optional[Dict[str, Any]] = None) -> None:
    """Autofill Booker customer name reliably.

    Priority:
    1) Keep an already-good extracted header value.
    2) Try OCR/text-layer extraction heuristics.
    3) Fall back to supplier template default_customer_name (learned from Admin edits).
    """
    try:
        cn = _collapse_spaces(str(header.get("customerName") or "").replace("|", " ").strip())
    except Exception:
        cn = str(header.get("customerName") or "").strip()

    bad = (not cn) or (cn.lower() == "customer") or bool(re.search(r"\bcustomer\b\s*$", cn, flags=re.I))
    bad = bad or ("|" in cn)

    # If header contains 'CUSTOMER NO' etc without the actual name, treat as bad
    if not bad and re.search(r"\bNO\b\s*[:\-]?\s*\d{3,}", cn, flags=re.I) and not re.search(r"\b(MR|MRS|MS|MISS)\b", cn, flags=re.I):
        bad = True

    if bad:
        txt = extracted_text or ""
        cand = ""

        # A) Capture from the first title-based segment on a line containing CUSTOMER
        if txt:
            for ln in [x.strip() for x in txt.splitlines() if x.strip()]:
                if re.search(r"\bCUSTOMER\b", ln, flags=re.I):
                    m = re.search(r"\b(MR|MRS|MS|MISS)\b\s+(.{2,80})$", ln, flags=re.I)
                    if m:
                        cand = (m.group(1) + " " + m.group(2)).strip()
                        break

        # B) Original extractor (covers 'Customer: <name>' and next-line cases)
        if not cand and txt:
            cand = _booker_extract_customer_name(txt)

        # C) Look for 'DELIVER TO' / 'ACCOUNT NAME' style anchors (some Booker layouts)
        if not cand and txt:
            lines = [ln.strip() for ln in txt.splitlines()]
            anchors = ("DELIVER TO", "DELIVERED TO", "ACCOUNT NAME", "ACCOUNT", "SITE")
            for i, ln in enumerate(lines):
                if any(a in ln.upper() for a in anchors):
                    # capture anything after ':' on same line
                    m = re.search(r":\s*(.{3,90})$", ln)
                    if m and not re.search(r"\b(INVOICE|DATE|NUMBER|TOTAL|VAT|NET|GROSS)\b", m.group(1), flags=re.I):
                        cand = m.group(1).strip()
                        break
                    # else check next non-empty line
                    for j in range(1, 4):
                        if i + j >= len(lines):
                            break
                        nxt = (lines[i + j] or "").strip()
                        if not nxt:
                            continue
                        if re.search(r"\b(INVOICE|DATE|NUMBER|TOTAL|VAT|NET|GROSS|DELIVERY|PO|P\.O\.)\b", nxt, flags=re.I):
                            continue
                        cand = nxt
                        break
                if cand:
                    break

        cand = _clean_booker_customer_name(_collapse_spaces(str(cand).replace("|", " ").strip()))
        # remove trailing 'Customer' word if OCR stuck it on the end
        cand = re.sub(r"\s+CUSTOMER\s*$", "", cand, flags=re.I).strip()

        # D) Template default customer name fallback (learned from admin edit)
        if not cand:
            try:
                cfg = (template_hit or {}).get("config") or {}
                dc = _collapse_spaces(str(cfg.get("default_customer_name") or "").strip())
                if dc:
                    cand = dc
            except Exception:
                pass

        if cand:
            header["customerName"] = cand
    else:
        header["customerName"] = _clean_booker_customer_name(cn)
    lines = [ln.strip() for ln in t.splitlines()]

    # 1) Direct 'Customer: <name>' style
    for ln in lines:
        if re.search(r"\bCUSTOMER\b", ln, flags=re.I):
            m = re.search(r"\bCUSTOMER\b\s*(?:NAME)?\s*[:\-]?\s*(.{3,120})$", ln, flags=re.I)
            if m:
                cand = _clean_booker_customer_name(m.group(1).strip())
                if cand:
                    return cand

    # 2) 'Customer' line followed by name on next line(s)
    for i, ln in enumerate(lines):
        if re.fullmatch(r"\s*CUSTOMER\s*(?:NAME)?\s*[:\-]?\s*", ln, flags=re.I) or re.fullmatch(r"\s*CUSTOMER\s*(?:NAME)?\s*", ln, flags=re.I):
            for j in range(1, 5):
                if i + j >= len(lines):
                    break
                nxt = (lines[i + j] or "").strip()
                if not nxt:
                    continue
                if re.search(r"\b(INVOICE|DATE|NUMBER|TOTAL|VAT|NET|GROSS|DELIVERY|PO|P\.O\.?|ORDER)\b", nxt, flags=re.I):
                    continue
                cand = _clean_booker_customer_name(nxt)
                if cand:
                    return cand

    # 3) Heuristic: first strong Mr/Mrs/Ms line (avoid address lines)
    for ln in lines:
        if re.search(r"\b(MR|MRS|MS|MISS)\b", ln, flags=re.I) and re.search(r"[A-Z]", ln):
            if re.search(r"\b(ROAD|STREET|AVENUE|LANE|LTD|LIMITED|UNIT|POSTCODE)\b", ln, flags=re.I):
                continue
            cand = _clean_booker_customer_name(ln)
            if cand:
                return cand

    return ""

"""Supplier candidate selection for purchase-invoice parsers.

OCR pipelines can produce native text, DocTR text and token-rebuilt text for the
same invoice. This module selects the most complete reconciled parse without any
Streamlit or database dependency.
"""

from __future__ import annotations

from typing import Any, Callable, Dict

from .parsers.bestway import looks_like_bestway_invoice, parse_bestway_invoice
from .parsers.tns import parse_tns_invoice


ParsedInvoice = Dict[str, Any]
Parser = Callable[[str], ParsedInvoice]
Detector = Callable[[str], bool]


def best_reconciled_supplier_parse(
    parser: Parser,
    detector: Detector,
    *text_candidates: str,
) -> ParsedInvoice:
    """Select by reconciliation, header quality, row count and net difference."""
    best: ParsedInvoice = {}
    best_score = (-1, -1, -1, float("-inf"))
    recovered_header: Dict[str, Any] = {}
    seen = set()

    for candidate in text_candidates:
        text = str(candidate or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        try:
            if not detector(text):
                continue
            parsed = parser(text) or {}
        except Exception:
            continue

        header = parsed.get("header") or {}
        items = list(parsed.get("items") or [])
        for field in ("customerName", "invoiceDate", "invoiceNumber", "orderNumber"):
            value = header.get(field)
            if str(value or "").strip() and not str(recovered_header.get(field) or "").strip():
                recovered_header[field] = value
        try:
            header_net = round(float(header.get("totalNet") or 0.0), 2)
            lines_net = round(sum(float(row.get("lineNet") or 0.0) for row in items), 2)
        except (TypeError, ValueError):
            header_net = lines_net = 0.0
        reconciled = int(bool(items) and header_net > 0 and abs(lines_net - header_net) <= 0.10)
        completeness = sum(
            bool(str(header.get(field) or "").strip())
            for field in ("customerName", "invoiceDate", "invoiceNumber")
        )
        score = (reconciled, completeness, len(items), -abs(lines_net - header_net))
        if score > best_score:
            best, best_score = parsed, score

    if best:
        header = best.get("header") or {}
        for field, value in recovered_header.items():
            if not str(header.get(field) or "").strip():
                header[field] = value
        best["header"] = header
    return best


def best_tns_purchase_invoice(*text_candidates: str) -> ParsedInvoice:
    """Select TNS output while accepting a header-only native first page."""
    def is_tns_candidate(text: str) -> bool:
        upper = str(text or "").upper()
        return (
            ("TNS WHOLESALE" in upper or "TNS RETAIL LIMITED" in upper)
            and "INVOICE" in upper
        )

    return best_reconciled_supplier_parse(
        parse_tns_invoice,
        is_tns_candidate,
        *text_candidates,
    )


def best_bestway_purchase_invoice(*text_candidates: str) -> ParsedInvoice:
    """Select the strongest Bestway parse across OCR representations."""
    return best_reconciled_supplier_parse(
        parse_bestway_invoice,
        looks_like_bestway_invoice,
        *text_candidates,
    )


"""Final authority routing for deterministic supplier parsers."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .parsers.bestway import bestway_text_from_ocr_tokens
from .parsers.dhamecha import (
    _dhamecha_parse_purchase_invoice as parse_dhamecha_invoice,
    looks_like_dhamecha_invoice,
)
from .reconciliation import best_bestway_purchase_invoice, best_tns_purchase_invoice


Invoice = Dict[str, Any]
AuthorityResult = Tuple[Invoice, List[Invoice], str, float] | None


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def accept_reconciled_candidate(
    parsed: Invoice,
    engine: str,
    tolerance: float = 0.10,
) -> AuthorityResult:
    """Return a candidate only when its rows reconcile to its printed net."""
    header = parsed.get("header") or {}
    items = list(parsed.get("items") or [])
    header_net = round(_number(header.get("totalNet")), 2)
    rows_net = round(sum(_number(row.get("lineNet")) for row in items), 2)
    if items and header_net > 0 and abs(rows_net - header_net) <= tolerance:
        return header, items, engine, rows_net
    return None


def select_supplier_authority(
    text_candidates: Sequence[str],
    ocr_tokens: Iterable[Invoice] = (),
) -> AuthorityResult:
    """Select reconciled TNS or Bestway output from all available OCR sources.

    Ordering deliberately preserves v1.18 behavior: a reconciled Bestway result
    supersedes TNS if both detectors unexpectedly match the same noisy text.
    """
    texts = tuple(str(value or "") for value in text_candidates)
    combined = "\n".join(texts)
    token_text = bestway_text_from_ocr_tokens(list(ocr_tokens or []))
    candidates = (
        (best_tns_purchase_invoice(*texts, combined), "local_tns"),
        (best_bestway_purchase_invoice(token_text, *texts, combined), "local_bestway"),
    )
    accepted: AuthorityResult = None
    for parsed, engine in candidates:
        result = accept_reconciled_candidate(parsed, engine)
        if result is not None:
            accepted = result
    return accepted


def select_dhamecha_authority(text_candidates: Sequence[str]) -> AuthorityResult:
    """Return modular Dhamecha output only when it reconciles to printed net."""
    seen = set()
    best: AuthorityResult = None
    best_rows = -1
    for candidate in text_candidates:
        text = str(candidate or "").strip()
        if not text or text in seen or not looks_like_dhamecha_invoice(text):
            continue
        seen.add(text)
        try:
            result = accept_reconciled_candidate(
                parse_dhamecha_invoice(text),
                "local_dhamecha_module",
            )
        except Exception:
            result = None
        if result is not None and len(result[1]) > best_rows:
            best, best_rows = result, len(result[1])
    return best

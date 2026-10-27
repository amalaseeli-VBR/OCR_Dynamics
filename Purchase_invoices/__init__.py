"""Stable supplier-parser API for the v1.18 purchase-invoice workflow."""

from __future__ import annotations

from typing import Any, Dict, Optional

from .parsers.bestway import (
    bestway_text_from_ocr_tokens,
    looks_like_bestway_invoice,
    parse_bestway_invoice,
)
from .parsers.booker import BookerCombinedRowParser
from .parsers.dhamecha import (
    _dhamecha_parse_purchase_invoice as parse_dhamecha_invoice,
    looks_like_dhamecha_invoice,
)
from .parsers.dwg import _parse_dwg_purchase_invoice as parse_dwg_invoice
from .parsers.parfetts import _parse_parfetts_purchase_invoice as parse_parfetts_invoice
from .parsers.tns import looks_like_tns_invoice, parse_tns_invoice
from .reconciliation import (
    best_bestway_purchase_invoice,
    best_reconciled_supplier_parse,
    best_tns_purchase_invoice,
)
from .authority import (
    accept_reconciled_candidate,
    select_dhamecha_authority,
    select_supplier_authority,
)
from .dhamecha_workflow import (
    normalize_line_vat_for_reconciliation,
    reduce_duplicate_rows_to_header,
    repair_header_totals,
)


SUPPLIER_PARSERS: Dict[str, Any] = {
    "BOOKER": BookerCombinedRowParser,
    "DHAMECHA": parse_dhamecha_invoice,
    "DWG": parse_dwg_invoice,
    "PARFETTS": parse_parfetts_invoice,
    "BESTWAY": parse_bestway_invoice,
    "TNS": parse_tns_invoice,
}


def get_supplier_parser(supplier: str) -> Optional[Any]:
    return SUPPLIER_PARSERS.get((supplier or "").strip().upper())


__all__ = [
    "BookerCombinedRowParser",
    "accept_reconciled_candidate",
    "bestway_text_from_ocr_tokens",
    "best_bestway_purchase_invoice",
    "best_reconciled_supplier_parse",
    "best_tns_purchase_invoice",
    "get_supplier_parser",
    "looks_like_bestway_invoice",
    "looks_like_dhamecha_invoice",
    "normalize_line_vat_for_reconciliation",
    "reduce_duplicate_rows_to_header",
    "repair_header_totals",
    "looks_like_tns_invoice",
    "parse_bestway_invoice",
    "parse_dhamecha_invoice",
    "parse_dwg_invoice",
    "parse_parfetts_invoice",
    "parse_tns_invoice",
    "select_supplier_authority",
    "select_dhamecha_authority",
]

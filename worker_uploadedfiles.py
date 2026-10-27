from __future__ import annotations

import argparse
import copy
from email import policy
from email.parser import BytesParser
from html import unescape
import importlib.util
import hashlib
import json
import os
import re
import sys
import time
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import sqlserver as _sqlserver
import uploaded_invoice_tracking as _upload_tracking


BASE_DIR = Path(__file__).resolve().parent
DOCMATE_PATH = BASE_DIR / "docmate_app_v1.18.001_r01_customer.py"

ISPROCESS_SUCCESS = 1
ISPROCESS_FAILED = 2
ISPROCESS_VAT_MISMATCH = 3
ISPROCESS_UNSUPPORTED = 4
ISPROCESS_SOURCE_UNAVAILABLE = 5
WORKER_BUILD = "2026-09-09-v118-all-invoice-numbers"
# Printed invoice VAT can differ by a few pence from the sum of line-level
# rounded VAT values, especially on larger Dhamecha invoices.
VAT_DIFF_EPS = 0.10


def _worker_gemini_sdk_status() -> str:
    checks = []
    for module_name, label in (
        ("google.genai", "google-genai"),
        ("google.generativeai", "google-generativeai"),
    ):
        try:
            available = importlib.util.find_spec(module_name) is not None
        except Exception as e:
            checks.append(f"{label}=error:{e}")
            continue
        checks.append(f"{label}={'yes' if available else 'no'}")
    return " ".join(checks)

EMAIL_UTILITY_SUPPLIER_KEYWORDS = {
    "DPD Local": ("DPD LOCAL", "INVOICE FROM DPD", ".I615", ".I616"),
    "Microsoft": ("MICROSOFT", "MICROSOFT 365", "OFFICE 365", "G118"),
    "Vonage UK": ("VONAGE", "MONTHLY CHARGE SUCCESSFUL"),
    "Smart Payment Technologies": ("SMART PAYMENT TECHNOLOGIES", "MONTHLY BILL", "BILL_VISU"),
    "Varlink": ("VARLINK", "INV-"),
}

EMAIL_UTILITY_CLASSIFY_KEYWORDS = (
    "UTILITY",
    "UTILITIES",
    "MONTHLY BILL",
    "MONTHLY CHARGE",
    "ERECEIPT",
    "E-RECEIPT",
    "SERVICE STATEMENT",
    "BUSINESS BASIC",
    "OFFICE 365",
    "MICROSOFT 365",
    "DPD LOCAL",
    "VONAGE",
    "SMART PAYMENT TECHNOLOGIES",
    "VARLINK",
    "MICROSOFT INVOICE",
    "G118",
    "PROMPT ACTION",
)

EMAIL_PURCHASE_LINE_KEYWORDS = (
    "BOOKER",
    "PARFETTS",
    "DHAMECHA",
    "DRINKS WHOLESALE",
)

SUPPORTED_DIRECT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".eml"}
UNSUPPORTED_IMAGE_EXTENSIONS = {".heic", ".heif"}
SUPPORTED_OCR_MIME_TYPES = {"application/pdf", "image/png", "image/jpeg", "message/rfc822", "text/plain"}


def _load_docmate_module():
    if not DOCMATE_PATH.exists():
        raise FileNotFoundError(f"DocMate module not found at {DOCMATE_PATH}")
    spec = importlib.util.spec_from_file_location("docmate_app", str(DOCMATE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load DocMate module spec")

    # Patch Streamlit cache decorators BEFORE loading the module.
    import streamlit as st  # type: ignore

    def _no_cache(func=None, **_kwargs):
        if func is None:
            def _decorator(f):
                return f
            return _decorator
        return func

    st.cache_data = _no_cache  # type: ignore[assignment]
    st.cache_resource = _no_cache  # type: ignore[assignment]
    # Some versions use internal cache_data_api - patch that too
    try:
        from streamlit.runtime.caching import cache_data_api  # type: ignore
        cache_data_api.cache_data = _no_cache  # type: ignore[assignment]
    except Exception:
        pass

    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def smoke_test_load() -> Tuple[bool, str]:
    """Load the standalone app without connecting to or consuming the queue."""
    try:
        module = _load_docmate_module()
        required = (
            "LocalInvoiceParser",
            "OcrConfig",
            "ocr_text_cached",
            "_pdf_text_layer",
            "_guess_mime_type",
            "_select_supplier_authority",
            "_calc_metrics",
        )
        missing = [name for name in required if not hasattr(module, name)]
        if missing:
            return False, "missing worker API: " + ", ".join(missing)
        return True, f"ok module={DOCMATE_PATH.name} build={WORKER_BUILD}"
    except Exception as exc:
        return False, str(exc)


def _docmate_source_mtime_ns() -> int:
    try:
        return int(DOCMATE_PATH.stat().st_mtime_ns)
    except OSError:
        return 0


def _reload_docmate_module_if_changed(docmate_mod, loaded_mtime_ns: int):
    current_mtime_ns = _docmate_source_mtime_ns()
    if current_mtime_ns and current_mtime_ns != loaded_mtime_ns:
        docmate_mod = _load_docmate_module()
        _sqlserver.log_event(f"Worker reloaded DocMate module after source update: {DOCMATE_PATH.name}")
    return docmate_mod, current_mtime_ns or loaded_mtime_ns


def _parse_cms_id_from_filename(name: str) -> str:
    base = os.path.basename(name or "")
    if not base:
        return ""
    stem = base.split(".")[0]
    prefix = stem.split("_")[0]
    m = re.match(r"^([A-Za-z]\d+)$", prefix)
    if m:
        return m.group(1).upper()
    return prefix.strip().upper()


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    return "\n".join(_collapse_email_spaces(line) for line in text.splitlines() if line.strip())


def _collapse_email_spaces(text: str) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", str(text or "")).strip()


def _extract_email_payload(file_path: str, docmate_mod) -> Dict[str, Any]:
    msg = BytesParser(policy=policy.default).parsebytes(Path(file_path).read_bytes())
    text_parts = []
    html_parts = []
    attachments = []

    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        disp = (part.get_content_disposition() or "").lower()
        name = part.get_filename() or ""

        if name or disp == "attachment":
            payload = part.get_payload(decode=True) or b""
            if payload:
                mime = ctype if ctype and ctype != "application/octet-stream" else docmate_mod._guess_mime_type(name, payload)
                attachments.append({"name": name or "attachment", "mime": mime, "bytes": payload})
            continue

        if ctype == "text/plain":
            try:
                text_parts.append(str(part.get_content() or ""))
            except Exception:
                pass
        elif ctype == "text/html":
            try:
                html_parts.append(_html_to_text(str(part.get_content() or "")))
            except Exception:
                pass

    body = "\n\n".join(p for p in text_parts if p.strip())
    if not body.strip():
        body = "\n\n".join(p for p in html_parts if p.strip())

    subject = str(msg.get("subject") or "")
    sender = str(msg.get("from") or "")
    return {
        "subject": subject,
        "from": sender,
        "body": body,
        "attachments": attachments,
        "context_text": (
            f"Email Subject: {subject}\n"
            f"Email From: {sender}\n\n"
            f"{body}"
        ).strip(),
    }


def _email_attachment_score(att: Dict[str, Any]) -> int:
    name = str(att.get("name") or "")
    name_u = name.upper()
    mime = str(att.get("mime") or "").lower()
    size = len(att.get("bytes") or b"")

    is_pdf = name_u.endswith(".PDF") or mime == "application/pdf"
    is_image = mime.startswith("image/") or name_u.endswith((".PNG", ".JPG", ".JPEG"))
    if not is_pdf and not is_image:
        return -100000
    if is_image and re.fullmatch(r"IMAGE\d+\.(PNG|JPG|JPEG)", name_u):
        return -100000
    if is_image and size < 100000:
        return -100000

    score = min(size // 1024, 20000)
    if is_pdf:
        score += 50000
    if re.search(r"\b(INVOICE|INV|BILL|RECEIPT|STATEMENT)\b", name_u):
        score += 15000
    if re.search(r"\b(LOGO|SIGNATURE|BANNER|ICON)\b", name_u):
        score -= 30000
    return score


def _select_invoice_attachment(email_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attachments = email_payload.get("attachments") or []
    if not attachments:
        return None
    ranked = sorted(attachments, key=_email_attachment_score, reverse=True)
    best = ranked[0]
    return best if _email_attachment_score(best) > 0 else None


def _infer_email_supplier(text: str) -> str:
    up = (text or "").upper()
    for supplier, needles in EMAIL_UTILITY_SUPPLIER_KEYWORDS.items():
        if any(needle in up for needle in needles):
            return supplier
    return ""


def _looks_like_email_utility(text: str) -> bool:
    up = (text or "").upper()
    return any(keyword in up for keyword in EMAIL_UTILITY_CLASSIFY_KEYWORDS)


def _normalise_doc_type(process_type: str, file_name: str, email_context: str = "") -> str:
    doc_type = (process_type or "").strip() or "Purchase Invoice"
    doc_type_u = doc_type.upper()
    if doc_type.lower() in {"expenses", "expense", "utilities", "utility"}:
        return "Expenses"
    if doc_type_u in {"PURCHASE", "PURCHASE INVOICE", "PI", "PURCHASE_INVOICE"}:
        return "Purchase Invoice"
    if doc_type_u in {"SERVICE", "SERVICE INVOICE", "SI", "SERVICE_INVOICE"}:
        return "Service Invoice"
    if file_name.lower().endswith(".eml"):
        up = (email_context or "").upper()
        if _looks_like_email_utility(up):
            return "Expenses"
        if re.search(r"\b(INVOICE|BILL|RECEIPT|STATEMENT)\b", up) and not any(k in up for k in EMAIL_PURCHASE_LINE_KEYWORDS):
            return "Expenses"
    return doc_type


def _unsupported_file_reason(file_name: str, mime: str) -> str:
    suffix = Path(str(file_name or "")).suffix.lower()
    mime_l = str(mime or "").lower()
    if suffix in UNSUPPORTED_IMAGE_EXTENSIONS or mime_l in {"image/heic", "image/heif"}:
        return "unsupported file: HEIC/HEIF images are not supported by the worker; convert to PDF/JPG/PNG"
    if suffix and suffix not in SUPPORTED_DIRECT_EXTENSIONS and mime_l not in SUPPORTED_OCR_MIME_TYPES:
        return f"unsupported file: {suffix} is not supported by the worker"
    if mime_l not in SUPPORTED_OCR_MIME_TYPES:
        return f"unsupported file: MIME type {mime_l or 'unknown'} is not supported by the worker"
    return ""


def _fetch_next_uploaded(conn) -> Optional[Dict[str, Any]]:
    sql = """
    SET NOCOUNT ON;
    BEGIN TRAN;
    ;WITH cte AS (
        SELECT TOP (1) *
        FROM dbo.UploadedFiles WITH (READPAST, UPDLOCK, ROWLOCK)
        WHERE IsProcess = 0
        ORDER BY UploadedTime ASC
    )
    UPDATE cte
    SET IsProcess = 9
    OUTPUT inserted.*;
    COMMIT TRAN;
    """
    cur = conn.cursor()
    rows = cur.execute(sql).fetchall()
    if not rows:
        return None
    row = rows[0]
    return {d[0]: row[i] for i, d in enumerate(cur.description)}


def _update_isprocess(conn, row_id: Any, status: int) -> None:
    cur = conn.cursor()
    cur.execute("UPDATE dbo.UploadedFiles SET IsProcess=? WHERE Id=?", (status, row_id))
    conn.commit()


def _ensure_uploadedfiles_tracking_columns(conn) -> None:
    sql = """
    IF COL_LENGTH('dbo.UploadedFiles', 'HeaderId') IS NULL
    BEGIN
        ALTER TABLE dbo.UploadedFiles ADD HeaderId INT NULL;
    END
    IF COL_LENGTH('dbo.UploadedFiles', 'LineIdStart') IS NULL
    BEGIN
        ALTER TABLE dbo.UploadedFiles ADD LineIdStart INT NULL;
    END
    IF COL_LENGTH('dbo.UploadedFiles', 'LineIdEnd') IS NULL
    BEGIN
        ALTER TABLE dbo.UploadedFiles ADD LineIdEnd INT NULL;
    END
    IF COL_LENGTH('dbo.UploadedFiles', 'InvoiceNumber') IS NULL
    BEGIN
        ALTER TABLE dbo.UploadedFiles ADD InvoiceNumber NVARCHAR(MAX) NULL;
    END
    ELSE IF COL_LENGTH('dbo.UploadedFiles', 'InvoiceNumber') <> -1
    BEGIN
        ALTER TABLE dbo.UploadedFiles ALTER COLUMN InvoiceNumber NVARCHAR(MAX) NULL;
    END
    IF COL_LENGTH('dbo.UploadedFiles', 'InvoiceDate') IS NULL
    BEGIN
        ALTER TABLE dbo.UploadedFiles ADD InvoiceDate NVARCHAR(64) NULL;
    END
    """
    cur = conn.cursor()
    cur.execute(sql)
    _upload_tracking.ensure_table(conn)
    conn.commit()


def _update_uploadedfile_tracking(
    conn,
    row_id: Any,
    status: int,
    header_id: Optional[int],
    line_id_start: Optional[int],
    line_id_end: Optional[int],
    invoice_number: Optional[str],
    invoice_date: Optional[str],
    invoice_references: Optional[list[Dict[str, Any]]] = None,
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE dbo.UploadedFiles
        SET IsProcess=?,
            HeaderId=?,
            LineIdStart=?,
            LineIdEnd=?,
            InvoiceNumber=?,
            InvoiceDate=?
        WHERE Id=?
        """,
        (
            status,
            header_id,
            line_id_start,
            line_id_end,
            (str(invoice_number or "").strip() or None),
            (str(invoice_date or "").strip() or None),
            row_id,
        ),
    )
    if invoice_references is not None:
        _upload_tracking.replace_links(conn, row_id, invoice_references)
    conn.commit()


def _copy_to_unprocessed(file_path: str) -> Optional[str]:
    if not file_path or not os.path.exists(file_path):
        return None
    src = Path(file_path)
    dest_dir = src.parent / "unprocessed"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    try:
        if dest.exists():
            ts = time.strftime("%Y%m%d_%H%M%S")
            dest = dest_dir / f"{src.stem}_{ts}{src.suffix}"
        shutil.copy2(str(src), str(dest))
        return str(dest)
    except Exception:
        return None


def _wait_for_file_stable(
    file_path: str,
    *,
    stable_checks: int = 3,
    check_interval_s: float = 1.0,
    max_wait_s: float = 20.0,
    min_age_s: float = 2.0,
) -> Tuple[bool, str]:
    """Wait until a file stops changing before OCR/parsing it.

    This protects the automation path from reading PDFs while they are still
    being copied into the watch folder, which can lead to partial extraction
    results and false VAT mismatches.
    """
    if not file_path:
        return False, "empty file path"

    deadline = time.perf_counter() + max(1.0, float(max_wait_s))
    last_sig = None
    stable_hits = 0

    while time.perf_counter() < deadline:
        if not os.path.exists(file_path):
            return False, "file not found"
        try:
            st = os.stat(file_path)
            sig = (int(st.st_size), int(st.st_mtime_ns))
            age_s = max(0.0, time.time() - float(st.st_mtime))
        except OSError as e:
            return False, f"os stat error: {e}"

        if sig == last_sig and age_s >= float(min_age_s):
            stable_hits += 1
            if stable_hits >= int(max(1, stable_checks)):
                return True, ""
        else:
            stable_hits = 0
            last_sig = sig

        time.sleep(max(0.2, float(check_interval_s)))

    try:
        st = os.stat(file_path)
        return False, (
            f"file still changing or too new after wait: size={int(st.st_size)} "
            f"mtime={int(st.st_mtime)}"
        )
    except OSError as e:
        return False, f"os stat error after wait: {e}"


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _has_meaningful_items(items: Any) -> bool:
    for it in items or []:
        if not isinstance(it, dict):
            continue
        line_net = abs(_safe_float(it.get("lineNet")))
        unit_price = abs(_safe_float(it.get("unitPrice")))
        qty = abs(_safe_float(it.get("qty")))
        code = str(it.get("code") or "").strip()
        desc = str(it.get("description") or "").strip()
        if line_net > 0.0 or unit_price > 0.0:
            return True
        if qty > 0.0 and (code or desc):
            return True
    return False


def _looks_unsupported_extraction(
    doc_type: str,
    header: Dict[str, Any],
    items: Any,
    metrics: Dict[str, Any],
    extracted_text: str,
) -> Tuple[bool, str]:
    supplier = str(header.get("supplierName") or "").strip()
    doc_number = str(header.get("invoiceNumber") or header.get("accountNumber") or header.get("customerNo") or "").strip()
    doc_date = str(header.get("invoiceDate") or "").strip()
    text_len = len((extracted_text or "").strip())
    has_items = _has_meaningful_items(items)
    gross = abs(_safe_float(metrics.get("gross_header")))
    net = abs(_safe_float(metrics.get("net_header")))
    vat = abs(_safe_float(metrics.get("vat_header")))
    has_totals = any(v > 0.0 for v in (gross, net, vat))

    if doc_type == "Expenses":
        if has_totals:
            return False, ""
        if supplier and (doc_number or doc_date):
            return False, ""
        if text_len < 40:
            return True, "unsupported file: no usable expense data extracted"
        return True, "unsupported file: expense header/totals not detected"

    if has_items or has_totals:
        return False, ""
    if supplier and (doc_number or doc_date):
        return False, ""
    if text_len < 40:
        return True, "unsupported file: no usable invoice data extracted"
    return True, "unsupported file: invoice header/items not detected"


def _fmt_timings(timings: Dict[str, float]) -> str:
    ordered = (
        "stable_wait_s",
        "read_s",
        "pdf_text_s",
        "ocr_s",
        "parse_s",
        "validate_s",
        "save_s",
        "total_s",
    )
    parts = []
    for key in ordered:
        if key in timings:
            parts.append(f"{key}={timings[key]:.3f}")
    return " timings[" + " ".join(parts) + "]" if parts else ""


def _prepare_purchase_invoice_for_reconciliation(
    docmate_mod,
    header: Dict[str, Any],
    items: Any,
) -> Tuple[Dict[str, Any], list[Dict[str, Any]]]:
    """Align worker reconciliation with the main app's save/UI pipeline."""
    header = dict(header or {})
    clean_items = []
    for it in items or []:
        if isinstance(it, dict):
            clean_items.append(dict(it))

    clean_items = docmate_mod._ensure_line_numbers(clean_items)
    try:
        clean_items = docmate_mod._fix_qty_outliers(clean_items)
    except Exception:
        pass
    try:
        clean_items = docmate_mod._fix_por_outliers(clean_items)
    except Exception:
        pass
    try:
        clean_items = docmate_mod._mark_void_items(clean_items)
    except Exception:
        pass
    try:
        vat_map = docmate_mod._derive_vat_map_from_items(clean_items)
        clean_items = docmate_mod._preview_enrich_items(clean_items, vat_map)
    except Exception:
        pass
    try:
        clean_items = docmate_mod._fix_pack_size_from_description(clean_items)
    except Exception:
        pass

    clean_items = docmate_mod._normalise_items_for_persistence(clean_items)
    docmate_mod._fill_header_totals_from_items(header, clean_items)
    try:
        header["invoiceDate"] = docmate_mod._normalise_invoice_date_str(header.get("invoiceDate") or "")
    except Exception:
        pass
    return header, clean_items


def _derive_uploadedfile_doc_refs(
    docmate_mod,
    doc_type: str,
    header: Dict[str, Any],
    fallback_name: str,
    fallback_text: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    doc_number = str(
        header.get("invoiceNumber")
        or header.get("invoice_number")
        or header.get("docNumber")
        or header.get("documentNumber")
        or header.get("document_no")
        or ""
    ).strip()
    if not doc_number and doc_type == "Expenses":
        doc_number = str(header.get("accountNumber") or header.get("customerNo") or "").strip()
    if not doc_number and doc_type != "Expenses":
        text = "\n".join(
            str(x or "")
            for x in (
                fallback_text,
                header.get("raw_text") if isinstance(header, dict) else "",
                header.get("debug_text") if isinstance(header, dict) else "",
                fallback_name,
            )
            if str(x or "").strip()
        )
        for pattern in (
            r"\b(?:invoice|inv)\s*(?:no\.?|number|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-\/]{1,30})\b",
            r"\b(?:document|doc)\s*(?:no\.?|number|#)?\s*[:#-]?\s*([A-Z0-9][A-Z0-9\-\/]{1,30})\b",
        ):
            m = re.search(pattern, text, flags=re.I)
            if not m:
                continue
            cand = str(m.group(1) or "").strip(" .,:;#-")
            if cand and cand.upper() not in {"NO", "NUMBER", "DATE", "TOTAL", "VAT", "INVOICE"} and re.search(r"\d", cand):
                doc_number = cand[:64]
                break
    if not doc_number and doc_type == "Expenses":
        doc_number = Path(fallback_name or "").stem[:64]
    doc_date = (
        docmate_mod._normalise_invoice_date_str(header.get("invoiceDate") or "")
        or str(header.get("invoiceDate") or "").strip()
    )
    return (doc_number or None), (doc_date or None)


def _candidate_score(docmate_mod, doc_type: str, header: Dict[str, Any], items: list[Dict[str, Any]], metrics: Dict[str, Any]) -> tuple:
    doc_number, doc_date = _derive_uploadedfile_doc_refs(docmate_mod, doc_type, header, "")
    if doc_type == "Expenses":
        total_penalty = abs(float(metrics.get("diff_gross", 0.0) or 0.0))
    else:
        total_penalty = (
            abs(float(metrics.get("diff_net", 0.0) or 0.0))
            + abs(float(metrics.get("diff_vat", 0.0) or 0.0))
            + abs(float(metrics.get("diff_gross", 0.0) or 0.0))
        )
    return (
        1 if doc_number else 0,
        1 if doc_date else 0,
        -round(total_penalty, 4),
        len(items or []),
        1 if abs(float(metrics.get("gross_header", 0.0) or 0.0)) > 0.0 else 0,
    )


def _booker_worker_cleanup(
    docmate_mod,
    header: Dict[str, Any],
    items: list[Dict[str, Any]],
    extracted_text: str,
) -> list[Dict[str, Any]]:
    supplier_u = str(header.get("supplierName") or "").upper()
    text_u = str(extracted_text or "").upper()
    if "BOOKER" not in supplier_u and "BOOKER" not in text_u:
        return items

    try:
        if hasattr(docmate_mod, "_booker_finalize_invoice_items"):
            items = docmate_mod._booker_finalize_invoice_items(items, extracted_text or "")
    except Exception:
        pass

    try:
        items = docmate_mod._booker_fix_free_lines(items)
    except Exception:
        pass

    promo_re = re.compile(
        r"\bBUY\s*ANY\b|\bBUY\s*\d+\b|\bBUY\d+\b|\bMULTI\b|\bPROMO\b|\bPROMOTION\b|"
        r"\bDEAL\b|\bOFFER\b|\bSAVE\b|\bSAVING\b|\bDISC(?:OUNT)?\b|\bGET\s*\d+\s*FREE\b",
        re.IGNORECASE,
    )

    try:
        promo_items = docmate_mod._booker_promo_items_from_text(extracted_text or "")
        if promo_items:
            # Rebuild Booker promo rows from the raw text so duplicated/partial promo
            # lines from the parser do not push reconciliation outside tolerance.
            items = [
                it for it in (items or [])
                if not (
                    isinstance(it, dict)
                    and (
                        str(it.get("code") or "").upper() == "PROMO"
                        or bool(promo_re.search(str(it.get("description") or "")))
                    )
                )
            ]
            existing = set()
            for pit in promo_items:
                promo_idx = pit.get("_promoLineIdx")
                if promo_idx is not None:
                    key = ("idx", int(promo_idx))
                else:
                    key = (
                        float(pit.get("lineNet") or 0.0),
                        str(pit.get("description") or "").upper().strip(),
                    )
                if key not in existing:
                    items.append(pit)
                    existing.add(key)
    except Exception:
        pass

    try:
        inv_no = re.sub(r"\D+", "", str(header.get("invoiceNumber") or ""))
        promo_nonzero_desc = {
            str(it.get("description") or "").strip()
            for it in items
            if isinstance(it, dict)
            and str(it.get("code") or "").upper() == "PROMO"
            and abs(float(it.get("lineNet") or 0.0)) > 0.0001
        }
        cleaned = []
        promo_re = re.compile(r"\bBUY\s*ANY\b|\bBUY\s*\d+\b|\bBUY\d+\b|\bMULTI\b|\bPROMO\b|\bPROMOTION\b|\bDEAL\b|\bOFFER\b|\bSAVE\b|\bSAVING\b|\bDISC(?:OUNT)?\b|\bGET\s*\d+\s*FREE\b", re.IGNORECASE)
        for it in items:
            if not isinstance(it, dict):
                continue
            code = re.sub(r"\D+", "", str(it.get("code") or ""))
            desc = str(it.get("description") or "").strip()
            desc = docmate_mod._norm_pack_tokens(desc)
            qty = docmate_mod._safe_float(it.get("qty"))
            unit_price = docmate_mod._safe_float(it.get("unitPrice"))
            line_net = docmate_mod._safe_float(it.get("lineNet"))
            is_promo = (str(it.get("code") or "").upper() == "PROMO") or bool(promo_re.search(desc or ""))
            if is_promo and abs(line_net) < 0.0001 and desc in promo_nonzero_desc:
                continue
            if not re.fullmatch(r"\d{5,7}", code or ""):
                if not is_promo:
                    continue
            if inv_no and code == inv_no:
                continue
            if not desc and (qty == 0 and unit_price == 0 and line_net == 0):
                continue
            cleaned.append(it)
        items = cleaned
    except Exception:
        pass

    try:
        if hasattr(docmate_mod, "_booker_finalize_invoice_items"):
            items = docmate_mod._booker_finalize_invoice_items(items, extracted_text or "")
        items = docmate_mod._ensure_line_numbers(items)
    except Exception:
        pass

    return items


def _booker_worker_align_header_to_line_totals(
    docmate_mod,
    header: Dict[str, Any],
    items: list[Dict[str, Any]],
    extracted_text: str,
) -> Dict[str, Any]:
    """Booker worker rescue for product-only headers.

    Automation can keep the VAT-breakdown product totals as the header while
    the finalized Booker line set already includes invoice-level charges. When
    that happens, the line totals are the safer source for validation/save.
    """
    header = dict(header or {})
    supplier_u = str(header.get("supplierName") or "").upper()
    text_u = str(extracted_text or "").upper()
    worker_booker_flag = bool(header.get("_worker_booker_detected") or header.get("_booker_header_override_used"))
    if not worker_booker_flag and "BOOKER" not in supplier_u and "BOOKER" not in text_u:
        return header
    try:
        line_vat = round(sum(docmate_mod._safe_float((it or {}).get("vatAmount")) for it in (items or []) if isinstance(it, dict)), 2)
        line_gross = round(sum(docmate_mod._safe_float((it or {}).get("lineGross")) for it in (items or []) if isinstance(it, dict)), 2)
        if line_gross <= 0:
            line_net = round(sum(docmate_mod._safe_float((it or {}).get("lineNet")) for it in (items or []) if isinstance(it, dict)), 2)
            line_gross = round(line_net + line_vat, 2)
        else:
            line_net = round(line_gross - line_vat, 2)
        hdr_vat = round(docmate_mod._safe_float(header.get("totalVat")), 2)
        hdr_gross = round(docmate_mod._safe_float(header.get("totalGross")), 2)
        hdr_net = round(docmate_mod._safe_float(header.get("totalNet")), 2)
        vat_diff = round(line_vat - hdr_vat, 2)
        gross_diff = round(line_gross - hdr_gross, 2)
        net_diff = round(line_net - hdr_net, 2)
        if line_vat > 0 and line_gross > 0 and (
            abs(vat_diff) > VAT_DIFF_EPS
            or abs(gross_diff) > VAT_DIFF_EPS
            or abs(net_diff) > VAT_DIFF_EPS
        ):
            header["_worker_header_totals_overridden"] = True
            header["_worker_original_totalNet"] = hdr_net
            header["_worker_original_totalVat"] = hdr_vat
            header["_worker_original_totalGross"] = hdr_gross
            header["_worker_original_diff_net"] = net_diff
            header["_worker_original_diff_vat"] = vat_diff
            header["_worker_original_diff_gross"] = gross_diff
            header["totalNet"] = line_net
            header["totalVat"] = line_vat
            header["totalGross"] = line_gross
    except Exception:
        pass
    return header


def _parse_candidate(
    docmate_mod,
    extracted_text: str,
    doc_type: str,
    inferred_supplier: str = "",
    *,
    apply_worker_repairs: bool = True,
) -> Tuple[Dict[str, Any], list[Dict[str, Any]], Dict[str, Any]]:
    # Use the same modular Bestway/TNS authority path as the Streamlit UI. The
    # worker has reconstructed OCR text but no Streamlit token cache, so pass an
    # empty token list; row-oriented OCR text is still parsed deterministically.
    authority = None
    if str(doc_type or "") in {"Purchase Invoice", "Dynamics Invoice"}:
        try:
            authority = docmate_mod._select_dhamecha_authority((extracted_text or "",))
            if authority is None:
                authority = docmate_mod._select_supplier_authority(
                    (extracted_text or "",),
                    (),
                )
        except Exception:
            authority = None
    if authority:
        authority_header, authority_items, authority_engine, _ = authority
        data = {
            "header": authority_header,
            "items": authority_items,
            "_worker_supplier_engine": authority_engine,
        }
    else:
        data = None
        if str(doc_type or '') in {'Purchase Invoice', 'Dynamics Invoice'}:
            shared_root = str(BASE_DIR.parent)
            if shared_root not in sys.path:
                sys.path.insert(0, shared_root)
            from docmate_core.convelio_parser import parse_convelio
            data = parse_convelio(extracted_text or '')
        if data is None:
            data = docmate_mod.LocalInvoiceParser.parse(extracted_text or "", doc_type)
    header = data.get("header") or {}
    items = data.get("items") or []
    if inferred_supplier and not str(header.get("supplierName") or "").strip():
        header["supplierName"] = inferred_supplier
    if doc_type == "Expenses":
        items = []
    else:
        try:
            template_hit = None
            if hasattr(docmate_mod, "_db_find_best_template"):
                template_hit = docmate_mod._db_find_best_template(extracted_text or "", "supplier_templates")
            if template_hit and hasattr(docmate_mod, "_apply_trained_supplier_template_agent"):
                header, items, used_template_agent = docmate_mod._apply_trained_supplier_template_agent(
                    header,
                    items,
                    extracted_text or "",
                    template_hit,
                )
                if used_template_agent:
                    header["extractionMode"] = "Trained Supplier Template Agent"
                supplier_name = str((template_hit or {}).get("supplier_name") or "").strip()
                if supplier_name and not str(header.get("supplierName") or "").strip():
                    header["supplierName"] = supplier_name
        except Exception:
            pass
        if apply_worker_repairs:
            header, items = _prepare_purchase_invoice_for_reconciliation(docmate_mod, header, items)
            items = _booker_worker_cleanup(docmate_mod, header, items, extracted_text)
            header = _booker_worker_align_header_to_line_totals(docmate_mod, header, items, extracted_text)
            items = docmate_mod._normalise_items_for_persistence(docmate_mod._ensure_line_numbers(items))
            docmate_mod._fill_header_totals_from_items(header, items)
        else:
            try:
                items = docmate_mod._ensure_line_numbers(list(items or []))
            except Exception:
                items = list(items or [])
    try:
        recon_items = docmate_mod._reconciliation_items(header, items, extracted_text)
    except Exception:
        recon_items = items
    metrics = docmate_mod._calc_metrics(header, recon_items)
    return header, items, metrics


def _worker_settings(docmate_mod) -> Dict[str, Any]:
    try:
        return docmate_mod._read_settings()
    except Exception:
        try:
            return docmate_mod._default_settings()
        except Exception:
            return {}


def _apply_worker_gemini_fallback(
    docmate_mod,
    settings: Dict[str, Any],
    *,
    doc_type: str,
    file_name: str,
    file_bytes: bytes,
    mime: str,
    header: Dict[str, Any],
    items: list[Dict[str, Any]],
    metrics: Dict[str, Any],
    extracted_text: str,
) -> Tuple[Dict[str, Any], list[Dict[str, Any]], Dict[str, Any], str, str]:
    """Use the app's AUTO Gemini fallback in the background worker before validation/save."""
    if not file_bytes or not hasattr(docmate_mod, "_maybe_apply_google_document_ai_fallback"):
        return header, items, metrics, extracted_text, ""
    fallback_settings = dict(settings or {})

    def _nearby_money_after_label(text: str, label_pattern: str) -> float:
        try:
            m = re.search(label_pattern, text or "", flags=re.I)
            if not m:
                return 0.0
            window = str(text or "")[m.end(): m.end() + 120]
            money = re.search(r"[£$]?\s*([0-9][0-9,]*\.[0-9]{2})", window)
            if money:
                return float(str(money.group(1)).replace(",", ""))
        except Exception:
            return 0.0
        return 0.0

    def _booker_printed_totals(text: str) -> Tuple[float, float, float]:
        net = vat = gross = 0.0
        try:
            net_matches = re.findall(r"TOTALS?\s*:\s*GOODS\s*([0-9][0-9,]*\.[0-9]{2})", text or "", flags=re.I)
            if net_matches:
                net = float(str(net_matches[-1]).replace(",", ""))
            if net <= 0:
                net = _nearby_money_after_label(text, r"\b(?:TOTAL\s*EX\s*VAT|NET\s*TOTAL)\b")
            vat = _nearby_money_after_label(text, r"\b(?:TOTAL\s*VAT|VAT\s*TOTAL|VAT)\b")
            gross = _nearby_money_after_label(text, r"\b(?:INVOICE\s*TOTAL|TOTAL\s*DUE|AMOUNT\s*DUE)\b")
            if gross <= 0 and net > 0 and vat > 0:
                gross = round(net + vat, 2)
        except Exception:
            return 0.0, 0.0, 0.0
        return round(net, 2), round(vat, 2), round(gross, 2)

    force_reason = ""
    try:
        text_for_detection = "\n".join(
            str(x or "")
            for x in (
                file_name,
                header.get("supplierName") if isinstance(header, dict) else "",
                extracted_text,
            )
        )
        try:
            is_booker = bool(docmate_mod._looks_like_booker_invoice(text_for_detection))
        except Exception:
            is_booker = "BOOKER" in text_for_detection.upper()
        code_lines = 0
        try:
            if hasattr(docmate_mod, "_count_code_lines"):
                code_lines = int(docmate_mod._count_code_lines(extracted_text or "") or 0)
        except Exception:
            code_lines = 0
        text_total_net = 0.0
        text_total_vat = 0.0
        text_total_gross = 0.0
        try:
            if hasattr(docmate_mod, "_booker_like_header_fallback"):
                text_header = docmate_mod._booker_like_header_fallback(text_for_detection) or {}
                text_total_net = float(docmate_mod._safe_float(text_header.get("total_ex_vat")))
                text_total_vat = float(docmate_mod._safe_float(text_header.get("total_vat")))
                text_total_gross = float(docmate_mod._safe_float(text_header.get("total_inc_vat")))
        except Exception:
            text_total_net = text_total_vat = text_total_gross = 0.0
        if text_total_gross <= 0.0:
            block_net, block_vat, block_gross = _booker_printed_totals(text_for_detection)
            text_total_net = text_total_net or block_net
            text_total_vat = text_total_vat or block_vat
            text_total_gross = text_total_gross or block_gross
        try:
            line_net = round(float((metrics or {}).get("net_items", (metrics or {}).get("sum_net", 0.0)) or 0.0), 2)
            line_vat = round(float((metrics or {}).get("vat_items", (metrics or {}).get("sum_vat", 0.0)) or 0.0), 2)
            line_gross = round(float((metrics or {}).get("gross_items", (metrics or {}).get("sum_gross", 0.0)) or 0.0), 2)
        except Exception:
            line_net = round(sum(docmate_mod._safe_float((it or {}).get("lineNet")) for it in (items or []) if isinstance(it, dict)), 2)
            line_vat = round(sum(docmate_mod._safe_float((it or {}).get("vatAmount")) for it in (items or []) if isinstance(it, dict)), 2)
            line_gross = round(sum(docmate_mod._safe_float((it or {}).get("lineGross")) for it in (items or []) if isinstance(it, dict)), 2)
            if line_gross <= 0:
                line_gross = round(line_net + line_vat, 2)
        text_total_mismatch = (
            text_total_gross > 0
            and line_gross > 0
            and (
                abs(text_total_gross - line_gross) > 1.0
                or (text_total_vat > 0 and abs(text_total_vat - line_vat) > VAT_DIFF_EPS)
                or (text_total_net > 0 and abs(text_total_net - line_net) > 1.0)
            )
        )
        if doc_type == "Purchase Invoice" and is_booker:
            try:
                _sqlserver.log_event(
                    "Worker Booker totals audit "
                    f"file={file_name} local_rows={len(items or [])} code_lines={code_lines} "
                    f"text_net={text_total_net:.2f} text_vat={text_total_vat:.2f} text_gross={text_total_gross:.2f} "
                    f"line_net={line_net:.2f} line_vat={line_vat:.2f} line_gross={line_gross:.2f}"
                )
            except Exception:
                pass
        original_diff_net = 0.0
        original_diff_vat = 0.0
        original_diff_gross = 0.0
        try:
            original_diff_net = abs(float((header or {}).get("_worker_original_diff_net") or 0.0))
            original_diff_vat = abs(float((header or {}).get("_worker_original_diff_vat") or 0.0))
            original_diff_gross = abs(float((header or {}).get("_worker_original_diff_gross") or 0.0))
        except Exception:
            original_diff_net = original_diff_vat = original_diff_gross = 0.0
        repaired_mismatch = bool((header or {}).get("_worker_header_totals_overridden")) and (
            original_diff_net > 1.0
            or original_diff_vat > VAT_DIFF_EPS
            or original_diff_gross > 1.0
        )
        if doc_type == "Purchase Invoice" and is_booker and (text_total_mismatch or repaired_mismatch):
            if text_total_mismatch:
                force_reason = (
                    "booker_text_total_mismatch "
                    f"local_rows={len(items or [])} code_lines={code_lines} "
                    f"text_net={text_total_net:.2f} text_vat={text_total_vat:.2f} text_gross={text_total_gross:.2f} "
                    f"line_net={line_net:.2f} line_vat={line_vat:.2f} line_gross={line_gross:.2f}"
                )
            else:
                force_reason = (
                    "booker_repaired_mismatch "
                    f"local_rows={len(items or [])} code_lines={code_lines} "
                    f"diff_net={original_diff_net:.2f} diff_vat={original_diff_vat:.2f} diff_gross={original_diff_gross:.2f}"
                )
            fallback_settings["_worker_force_cloud_gemini_fallback"] = force_reason
            fallback_settings["auto_cloud_fallback_enabled"] = True
            try:
                _sqlserver.log_event(f"Worker forcing Gemini fallback {force_reason} file={file_name}")
            except Exception:
                pass
    except Exception:
        pass
    try:
        if hasattr(docmate_mod, "_assess_extraction_confidence"):
            confidence = docmate_mod._assess_extraction_confidence(
                header or {},
                items or [],
                doc_type,
                raw_text=extracted_text or "",
                engine_used="worker_local_doctr",
            )
            score = int((confidence or {}).get("score") or 0)
            fallback_settings["_worker_local_confidence_score"] = score
            if score < int(fallback_settings.get("auto_confidence_review_threshold", 65) or 65):
                fallback_settings["auto_cloud_fallback_enabled"] = True
    except Exception:
        pass
    try:
        header2, items2, engine2, text2 = docmate_mod._maybe_apply_google_document_ai_fallback(
            fallback_settings,
            dict(header or {}),
            list(items or []),
            doc_type,
            raw_text=extracted_text or "",
            engine_used="worker_local_doctr",
            file_bytes=file_bytes,
            mime_type=mime or "application/octet-stream",
            file_name=file_name,
        )
    except Exception as e:
        return header, items, metrics, extracted_text, f"gemini_fallback_error:{e}"

    if str(engine2 or "") != "cloud_gemini":
        if force_reason:
            return header, items, metrics, extracted_text, f"gemini_fallback_not_used:{force_reason}"
        return header, items, metrics, extracted_text, ""

    try:
        if hasattr(docmate_mod, "_restore_cloud_authoritative_header"):
            header2 = docmate_mod._restore_cloud_authoritative_header(header2, copy.deepcopy(header2))
    except Exception:
        pass
    try:
        items2 = docmate_mod._recalc_items_for_engine(copy.deepcopy(items2 or []), "cloud_gemini")
    except Exception:
        try:
            items2 = docmate_mod._recalc_items(copy.deepcopy(items2 or []))
        except Exception:
            items2 = list(items2 or [])
    try:
        recon_items = docmate_mod._reconciliation_items(header2, items2, text2 or extracted_text or "")
    except Exception:
        recon_items = items2
    try:
        metrics2 = docmate_mod._calc_metrics(header2, recon_items)
    except Exception:
        metrics2 = metrics
    return header2, items2, metrics2, (text2 or extracted_text or ""), "gemini_fallback_used"


def _apply_worker_purchase_table_override(
    docmate_mod,
    settings: Dict[str, Any],
    *,
    doc_type: str,
    file_name: str,
    header: Dict[str, Any],
    items: list[Dict[str, Any]],
    metrics: Dict[str, Any],
    extracted_text: str,
    pdf_text: str = "",
    ocr_text: str = "",
    ocr_cache_key: str = "",
) -> Tuple[Dict[str, Any], list[Dict[str, Any]], Dict[str, Any], str]:
    """Run the app's OCR-table fallback in the worker for Booker purchase invoices."""
    if doc_type != "Purchase Invoice":
        return header, items, metrics, ""

    text_for_detection = "\n".join(
        str(x or "")
        for x in (
            file_name,
            header.get("supplierName") if isinstance(header, dict) else "",
            extracted_text,
            pdf_text,
            ocr_text,
        )
    )
    try:
        is_booker = bool(docmate_mod._looks_like_booker_invoice(text_for_detection))
    except Exception:
        is_booker = "BOOKER" in text_for_detection.upper()
    if not is_booker:
        return header, items, metrics, ""

    try:
        ocr_lines = []
        if ocr_cache_key:
            ocr_lines = list(getattr(docmate_mod, "_OCR_LINES_CACHE", {}).get(ocr_cache_key) or [])
        combined_text = "\n".join(
            str(x or "")
            for x in (
                file_name,
                extracted_text,
                ocr_text,
                pdf_text,
            )
            if str(x or "").strip()
        )
        res = {
            "file": file_name,
            "doc_type": doc_type,
            "header": dict(header or {}),
            "items": list(items or []),
            "text": combined_text,
            "debug_text": combined_text,
            "text_hint": pdf_text or combined_text,
            "line_text": ocr_text or extracted_text or "",
            "ocr_cache_key": ocr_cache_key or "",
            "ocr_lines_snapshot": ocr_lines,
            "confidence": {
                "score": 0,
                "blockers": ["worker_booker_forced_purchase_table_override"],
            },
        }
        override_items, override_used = docmate_mod._purchase_table_override(res, settings or {}, force=True)
        if not override_used or not override_items:
            audit = res.get("_purchase_table_audit") or {}
            header2 = dict(header or {})
            header2["_worker_booker_detected"] = True
            if not str(header2.get("supplierName") or "").strip():
                header2["supplierName"] = "Booker"
            try:
                if hasattr(docmate_mod, "_booker_finalize_invoice_items"):
                    items2 = docmate_mod._booker_finalize_invoice_items(items, combined_text)
                else:
                    items2 = items
            except Exception:
                items2 = items
            header2 = _booker_worker_align_header_to_line_totals(docmate_mod, header2, items2, combined_text)
            try:
                recon_items = docmate_mod._reconciliation_items(header2, items2, combined_text)
            except Exception:
                recon_items = items2
            try:
                metrics2 = docmate_mod._calc_metrics(header2, recon_items)
            except Exception:
                metrics2 = metrics
            return header2, items2, metrics2, f"booker_purchase_table_not_used:{audit.get('reason') or 'unknown'}"

        items2 = list(override_items or [])
        header2 = dict(header or {})
        header2["_worker_booker_detected"] = True
        if not str(header2.get("supplierName") or "").strip():
            header2["supplierName"] = "Booker"
        override_header = dict(res.get("_purchase_table_override_header") or {})
        try:
            header2 = docmate_mod._apply_booker_header_override(header2, override_header, items2)
        except Exception:
            for key in ("invoiceNumber", "invoiceDate", "totalNet", "totalVat", "totalGross"):
                if override_header.get(key):
                    header2[key] = override_header.get(key)

        header2, items2 = _prepare_purchase_invoice_for_reconciliation(docmate_mod, header2, items2)
        try:
            if hasattr(docmate_mod, "_booker_finalize_invoice_items"):
                items2 = docmate_mod._booker_finalize_invoice_items(
                    items2,
                    combined_text,
                )
        except Exception:
            pass
        header2 = _booker_worker_align_header_to_line_totals(docmate_mod, header2, items2, combined_text)
        try:
            recon_items = docmate_mod._reconciliation_items(header2, items2, combined_text)
        except Exception:
            recon_items = items2
        metrics2 = docmate_mod._calc_metrics(header2, recon_items)
        audit = res.get("_purchase_table_audit") or {}
        return header2, items2, metrics2, f"booker_purchase_table_used:{audit.get('mapped_count') or len(items2)}"
    except Exception as e:
        return header, items, metrics, f"booker_purchase_table_error:{e}"


def _split_worker_pdf_docs(
    docmate_mod,
    file_name: str,
    pdf_bytes: bytes,
    mime: str,
) -> list[Dict[str, Any]]:
    if mime != "application/pdf" or not pdf_bytes:
        return []
    try:
        return docmate_mod._split_pdf_self_contained_invoices(
            {
                "idx": 0,
                "name": file_name,
                "bytes": pdf_bytes,
                "mime": mime,
            }
        ) or []
    except Exception:
        return []


def _save_worker_result(
    docmate_mod,
    *,
    doc_type: str,
    header: Dict[str, Any],
    items: list[Dict[str, Any]],
    metrics: Dict[str, Any],
    cfg_save: Dict[str, str],
    cms_customer_id: str,
    source_file_name: str,
    ocr_file_name: str,
    extracted_text: str = "",
    saved_reference: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Optional[int], Optional[int], Optional[int], Optional[str], Optional[str]]:
    doc_number, doc_date = _derive_uploadedfile_doc_refs(
        docmate_mod,
        doc_type,
        header,
        ocr_file_name or source_file_name,
        fallback_text=extracted_text,
    )
    if doc_number and not str(header.get("invoiceNumber") or "").strip() and doc_type != "Expenses":
        header["invoiceNumber"] = doc_number
        try:
            _sqlserver.log_event(f"Worker salvaged missing invoice number as {doc_number} for file={ocr_file_name or source_file_name}")
        except Exception:
            pass
    if doc_type != "Expenses" and not doc_number:
        try:
            _sqlserver.log_event(f"Worker missing document number; not saving file={ocr_file_name or source_file_name} doc_type={doc_type}")
        except Exception:
            pass
        return (
            False,
            f"missing document number for {doc_type}; header/lines not saved",
            None,
            None,
            None,
            None,
            doc_date or None,
        )
    header_sql = {
        "created_utc": docmate_mod._uk_now_iso(),
        "filename": source_file_name,
        "doc_type": doc_type,
        "supplier_name": docmate_mod._normalise_supplier_name(header.get("supplierName") or ""),
        "customer_name": str(header.get("customerName") or "").strip(),
        "cms_customer_id": cms_customer_id,
        "doc_number": doc_number,
        "doc_date": doc_date,
        "total_ex_vat": float(metrics.get("net_header") or 0.0),
        "total_vat": float(metrics.get("vat_header") or 0.0),
        "total_inc_vat": float(metrics.get("gross_header") or 0.0),
        "currency": str(header.get("currency") or "GBP"),
        "raw_header_json": json.dumps(header, ensure_ascii=False, default=str),
    }
    line_content = docmate_mod._invoice_line_content(items or [])
    fingerprint_payload = {
        "header": [doc_type, header_sql["supplier_name"], header_sql["customer_name"], cms_customer_id,
                   doc_number, doc_date, round(float(header_sql["total_ex_vat"]), 6),
                   round(float(header_sql["total_vat"]), 6), round(float(header_sql["total_inc_vat"]), 6),
                   header_sql["currency"]],
        "lines": line_content,
    }
    header_sql["invoice_fingerprint"] = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()

    if doc_type == "Expenses":
        expenses_header_sql = {
            **header_sql,
            "source_attachment": ocr_file_name if ocr_file_name != source_file_name else "",
            "account_number": str(header.get("accountNumber") or "").strip(),
            "contract_number": str(header.get("contractNumber") or "").strip(),
            "customer_no": str(header.get("customerNo") or "").strip(),
            "site_address": str(header.get("siteAddress") or "").strip(),
            "bill_to_address": str(header.get("billToAddress") or "").strip(),
            "service_period": str(header.get("servicePeriod") or "").strip(),
            "payment_due_date": docmate_mod._normalise_invoice_date_str(header.get("paymentDueDate") or "")
            or str(header.get("paymentDueDate") or "").strip(),
            "payment_due_text": str(header.get("paymentDueText") or "").strip(),
            "utility_type": str(header.get("utilityType") or "").strip(),
            "meter_number": str(header.get("meterNumber") or "").strip(),
            "mpan": str(header.get("mpan") or "").strip(),
            "mprn": str(header.get("mprn") or "").strip(),
            "vat_registration_number": str(header.get("vatRegistrationNumber") or "").strip(),
            "extraction_mode": str(header.get("extractionMode") or "Utility Header Agent").strip(),
            "raw_header_json": json.dumps(header, ensure_ascii=False, default=str),
        }
        ok_h, msg_h = _sqlserver.insert_expenses_header_sqlserver(expenses_header_sql, cfg_override=cfg_save)
        if not ok_h:
            return False, f"sqlserver expenses save failed: header_ok={ok_h} | {msg_h}", None, None, None, doc_number or None, doc_date or None
        header_id = _sqlserver.get_expenses_header_id(expenses_header_sql, cfg_override=cfg_save)
        return True, "ok", header_id, None, None, doc_number or None, doc_date or None

    ok_h, msg_h = _sqlserver.insert_invoice_header_sqlserver(header_sql, cfg_override=cfg_save)
    if ok_h and str(msg_h).lower() == "duplicate":
        ok_l, msg_l = True, "duplicate skipped"
    else:
        ok_l, msg_l = _sqlserver.insert_invoice_lines_sqlserver(header_sql, items, cfg_override=cfg_save)
    if not ok_h or not ok_l:
        return False, f"sqlserver save failed: header_ok={ok_h} lines_ok={ok_l} | {msg_h} | {msg_l}", None, None, None, doc_number or None, doc_date or None
    header_id = _sqlserver.get_invoice_header_id(header_sql, cfg_override=cfg_save)
    line_id_start, line_id_end = _sqlserver.get_invoice_line_id_range(header_sql, cfg_override=cfg_save)
    if header_id is None or (items and (line_id_start is None or line_id_end is None)):
        return False, "saved invoice references could not be resolved", None, None, None, doc_number, doc_date
    if saved_reference is not None:
        saved_reference.update({
            "database": cfg_save.get("database") or "",
            "doc_type": doc_type, "header_id": header_id,
            "fingerprint": header_sql["invoice_fingerprint"],
            "invoice_number": doc_number, "invoice_date": doc_date,
            "line_id_start": line_id_start, "line_id_end": line_id_end,
        })
    result_message = "duplicate skipped" if str(msg_h).lower() == "duplicate" else "ok"
    _sqlserver.log_event("Worker invoice persistence " + json.dumps({
        "database": cfg_save.get("database") or "",
        "invoice_number": doc_number,
        "invoice_date": doc_date,
        "header_result": msg_h,
        "line_result": msg_l,
        "header_id": header_id,
        "line_id_start": line_id_start,
        "line_id_end": line_id_end,
        "invoice_fingerprint": header_sql["invoice_fingerprint"],
        "upload_filename": source_file_name,
    }, ensure_ascii=False, default=str))
    return True, result_message, header_id, line_id_start, line_id_end, doc_number or None, doc_date or None


def _process_file(
    docmate_mod,
    file_path: str,
    cms_customer_id: str,
    process_type: str,
    cfg_save: Dict[str, str],
    save_callback=None,
    invoice_references: Optional[list[Dict[str, Any]]] = None,
    endpoint_review: bool = False,
) -> Tuple[bool, str, Optional[int], Optional[int], Optional[int], Optional[str], Optional[str], int]:
    # Convenience uses the existing SQL save path. Other queue workers (for
    # example Dynamics) may reuse the OCR/validation pipeline with their own
    # destination tables by supplying a compatible callback.
    if save_callback is None:
        save_callback = _save_worker_result
    t0 = time.perf_counter()
    timings: Dict[str, float] = {}
    if not file_path or not os.path.exists(file_path):
        return False, f"source unavailable: file not found: {file_path}", None, None, None, None, None, ISPROCESS_SOURCE_UNAVAILABLE

    t_stable = time.perf_counter()
    stable_ok, stable_reason = _wait_for_file_stable(file_path)
    timings["stable_wait_s"] = time.perf_counter() - t_stable
    if not stable_ok:
        timings["total_s"] = time.perf_counter() - t0
        return False, f"source unavailable: {stable_reason}" + _fmt_timings(timings), None, None, None, None, None, ISPROCESS_SOURCE_UNAVAILABLE

    file_name = os.path.basename(file_path)
    try:
        file_bytes = Path(file_path).read_bytes()
    except FileNotFoundError:
        timings["total_s"] = time.perf_counter() - t0
        return False, f"source unavailable: file not found: {file_path}" + _fmt_timings(timings), None, None, None, None, None, ISPROCESS_SOURCE_UNAVAILABLE
    except PermissionError as e:
        timings["total_s"] = time.perf_counter() - t0
        return False, f"source unavailable: access denied/quarantined: {e}" + _fmt_timings(timings), None, None, None, None, None, ISPROCESS_SOURCE_UNAVAILABLE
    except OSError as e:
        timings["total_s"] = time.perf_counter() - t0
        msg = str(e).lower()
        if any(tok in msg for tok in ("access is denied", "permission denied", "being used by another process")):
            return False, f"source unavailable: access denied/quarantined: {e}" + _fmt_timings(timings), None, None, None, None, None, ISPROCESS_SOURCE_UNAVAILABLE
        return False, f"source unavailable: os error reading file: {e}" + _fmt_timings(timings), None, None, None, None, None, ISPROCESS_SOURCE_UNAVAILABLE
    timings["read_s"] = time.perf_counter() - t0
    source_file_name = file_name
    ocr_file_name = file_name
    ocr_bytes = file_bytes
    mime = docmate_mod._guess_mime_type(file_name, file_bytes)
    unsupported_reason = _unsupported_file_reason(ocr_file_name, mime)
    if unsupported_reason and not file_name.lower().endswith(".eml"):
        timings["total_s"] = time.perf_counter() - t0
        return False, unsupported_reason + _fmt_timings(timings), None, None, None, None, None, ISPROCESS_UNSUPPORTED
    settings = _worker_settings(docmate_mod)
    worker_ocr_max_pages = max(
        int(settings.get("ocr_max_pages", 10) or 10),
        int(settings.get("booker_max_pages", 4) or 4),
    )
    email_context = ""
    inferred_supplier = ""

    if file_name.lower().endswith(".eml"):
        email_payload = _extract_email_payload(file_path, docmate_mod)
        selected_attachment = _select_invoice_attachment(email_payload)
        email_context = str(email_payload.get("context_text") or "")
        if selected_attachment:
            ocr_file_name = str(selected_attachment.get("name") or file_name)
            ocr_bytes = selected_attachment.get("bytes") or b""
            mime = docmate_mod._guess_mime_type(ocr_file_name, ocr_bytes)
            email_context = (
                f"{email_context}\n\n"
                f"Selected invoice attachment: {ocr_file_name}"
            ).strip()
        else:
            ocr_file_name = file_name
            ocr_bytes = b""
            mime = "text/plain"
        inferred_supplier = _infer_email_supplier(
            f"{source_file_name}\n{ocr_file_name}\n{email_context}"
        )

    unsupported_reason = _unsupported_file_reason(ocr_file_name, mime)
    if unsupported_reason:
        timings["total_s"] = time.perf_counter() - t0
        return False, unsupported_reason + _fmt_timings(timings), None, None, None, None, None, ISPROCESS_UNSUPPORTED

    extracted_text = ""
    pdf_text = ""
    ocr_text = ""
    cache_key = ""
    if ocr_bytes:
        if mime == "application/pdf":
            t_pdf = time.perf_counter()
            try:
                pdf_text = docmate_mod._pdf_text_layer(ocr_bytes, max_pages=10)
            except Exception:
                pdf_text = ""
            timings["pdf_text_s"] = time.perf_counter() - t_pdf
            if (pdf_text or "").strip():
                extracted_text = pdf_text
        cfg = docmate_mod.OcrConfig(
            engine="doctr",
            tesseract_cmd="",
            dpi=int(settings.get("ocr_dpi", 250) or 250),
            max_pages=worker_ocr_max_pages,
            force_pdf=False,
            auto_install=True,
        )
        cache_key = docmate_mod._sha256_bytes(ocr_bytes) + f"|{cfg.engine}|{cfg.dpi}|{cfg.max_pages}|{cfg.force_pdf}"
        ocr_fn = getattr(docmate_mod.ocr_text_cached, "__wrapped__", docmate_mod.ocr_text_cached)
        t_ocr = time.perf_counter()
        ocr_text = ocr_fn(
            cache_key,
            mime,
            docmate_mod.dataclasses.asdict(cfg),
            ocr_bytes,
            progress_cb=None,
            progress_label=ocr_file_name,
        )
        timings["ocr_s"] = time.perf_counter() - t_ocr
        extracted_text = ocr_text
        if not (extracted_text or "").strip() and mime == "application/pdf":
            extracted_text = pdf_text
        if mime == "application/pdf" and (pdf_text or "").strip():
            try:
                prefer_pdf_text = bool(
                    docmate_mod._looks_like_booker_invoice(
                        "\n".join(str(x or "") for x in (file_name, inferred_supplier, pdf_text))
                    )
                )
            except Exception:
                prefer_pdf_text = "BOOKER" in f"{file_name}\n{inferred_supplier}\n{pdf_text}".upper()
            if prefer_pdf_text:
                extracted_text = pdf_text
    if email_context:
        extracted_text = (
            f"{email_context}\n\n"
            f"Attachment Text:\n{extracted_text or ''}"
        ).strip()

    doc_type = _normalise_doc_type(process_type, file_name, f"{email_context}\n{ocr_file_name}")
    split_docs = []
    if doc_type == "Purchase Invoice":
        split_docs = _split_worker_pdf_docs(docmate_mod, ocr_file_name, ocr_bytes, mime)
        if split_docs:
            split_numbers = ", ".join(
                str(doc.get("detected_invoice_number") or "").strip()
                for doc in split_docs
                if str(doc.get("detected_invoice_number") or "").strip()
            )
            _sqlserver.log_event(
                f"Worker split PDF file={ocr_file_name} invoices={len(split_docs)}"
                f" numbers={split_numbers}"
            )

    parse_jobs: list[Dict[str, Any]] = []
    if split_docs:
        for idx, split_doc in enumerate(split_docs, start=1):
            split_bytes = split_doc.get("bytes") or b""
            split_name = str(split_doc.get("name") or f"{Path(ocr_file_name).stem}_part_{idx}.pdf")
            split_pdf_text = ""
            split_ocr_text = ""
            if split_bytes:
                try:
                    split_pdf_text = docmate_mod._pdf_text_layer(split_bytes, max_pages=10)
                except Exception:
                    split_pdf_text = ""
                cfg_split = docmate_mod.OcrConfig(
                    engine="doctr",
                    tesseract_cmd="",
                    dpi=int(settings.get("ocr_dpi", 250) or 250),
                    max_pages=worker_ocr_max_pages,
                    force_pdf=False,
                    auto_install=True,
                )
                split_cache_key = docmate_mod._sha256_bytes(split_bytes) + f"|{cfg_split.engine}|{cfg_split.dpi}|{cfg_split.max_pages}|{cfg_split.force_pdf}"
                split_ocr_fn = getattr(docmate_mod.ocr_text_cached, "__wrapped__", docmate_mod.ocr_text_cached)
                split_ocr_text = split_ocr_fn(
                    split_cache_key,
                    "application/pdf",
                    docmate_mod.dataclasses.asdict(cfg_split),
                    split_bytes,
                    progress_cb=None,
                    progress_label=split_name,
                )
            split_text = split_ocr_text or split_pdf_text
            if split_pdf_text.strip():
                try:
                    prefer_split_pdf_text = bool(
                        docmate_mod._looks_like_booker_invoice(
                            "\n".join(str(x or "") for x in (split_name, inferred_supplier, split_pdf_text))
                        )
                    )
                except Exception:
                    prefer_split_pdf_text = "BOOKER" in f"{split_name}\n{inferred_supplier}\n{split_pdf_text}".upper()
                if prefer_split_pdf_text:
                    split_text = split_pdf_text
            parse_jobs.append(
                {
                    "file_name": split_name,
                    "source_file_name": source_file_name,
                    "file_bytes": split_bytes,
                    "mime": "application/pdf",
                    "extracted_text": split_text,
                    "pdf_text": split_pdf_text,
                    "ocr_text": split_ocr_text,
                    "ocr_cache_key": split_cache_key if split_bytes else "",
                    "inferred_supplier": inferred_supplier,
                }
            )
    else:
        parse_jobs.append(
            {
                "file_name": ocr_file_name,
                "source_file_name": source_file_name,
                "file_bytes": ocr_bytes,
                "mime": mime,
                "extracted_text": extracted_text,
                "pdf_text": pdf_text,
                "ocr_text": ocr_text,
                "ocr_cache_key": cache_key,
                "inferred_supplier": inferred_supplier,
            }
        )

    t_parse = time.perf_counter()
    parsed_results: list[Dict[str, Any]] = []
    for job in parse_jobs:
        job_text = str(job.get("extracted_text") or "")
        job_pdf_text = str(job.get("pdf_text") or "")
        job_ocr_text = str(job.get("ocr_text") or "")
        job_all_text = "\n".join(
            str(x or "")
            for x in (
                str(job.get("file_name") or ""),
                job_text,
                job_ocr_text,
                job_pdf_text,
            )
            if str(x or "").strip()
        )
        job_ocr_cache_key = str(job.get("ocr_cache_key") or "")
        job_inferred_supplier = str(job.get("inferred_supplier") or "")
        raw_header, raw_items, raw_metrics = _parse_candidate(
            docmate_mod,
            job_text,
            doc_type,
            inferred_supplier=job_inferred_supplier,
            apply_worker_repairs=False,
        )
        header, items, metrics = _parse_candidate(
            docmate_mod,
            job_text,
            doc_type,
            inferred_supplier=job_inferred_supplier,
            apply_worker_repairs=True,
        )
        if job.get("mime") == "application/pdf" and job_pdf_text.strip() and job_ocr_text.strip():
            raw_pdf_header, raw_pdf_items, raw_pdf_metrics = _parse_candidate(
                docmate_mod,
                job_pdf_text,
                doc_type,
                inferred_supplier=job_inferred_supplier,
                apply_worker_repairs=False,
            )
            pdf_header, pdf_items, pdf_metrics = _parse_candidate(
                docmate_mod,
                job_pdf_text,
                doc_type,
                inferred_supplier=job_inferred_supplier,
                apply_worker_repairs=True,
            )
            if _candidate_score(docmate_mod, doc_type, pdf_header, pdf_items, pdf_metrics) > _candidate_score(docmate_mod, doc_type, header, items, metrics):
                raw_header, raw_items, raw_metrics = raw_pdf_header, raw_pdf_items, raw_pdf_metrics
                header, items, metrics = pdf_header, pdf_items, pdf_metrics
                job_text = job_pdf_text
                job_all_text = "\n".join(
                    str(x or "")
                    for x in (
                        str(job.get("file_name") or ""),
                        job_text,
                        job_ocr_text,
                        job_pdf_text,
                    )
                    if str(x or "").strip()
                )
        # Keep one canonical text representation for parsing.  job_all_text is
        # audit/evidence text and can contain the same invoice up to three times
        # (job_text + OCR + PDF text).  Passing it through the no-op Gemini path
        # caused the subsequent local reparse to multiply Parfetts rows/totals.
        header, items, metrics, job_text, gemini_note = _apply_worker_gemini_fallback(
            docmate_mod,
            settings,
            doc_type=doc_type,
            file_name=str(job.get("file_name") or ""),
            file_bytes=bytes(job.get("file_bytes") or b""),
            mime=str(job.get("mime") or ""),
            header=raw_header,
            items=raw_items,
            metrics=raw_metrics,
            extracted_text=job_text,
        )
        if gemini_note != "gemini_fallback_used":
            header, items, metrics = _parse_candidate(
                docmate_mod,
                job_text,
                doc_type,
                inferred_supplier=job_inferred_supplier,
                apply_worker_repairs=True,
            )
        if gemini_note:
            try:
                _sqlserver.log_event(f"Worker {gemini_note} file={job.get('file_name') or ''}")
            except Exception:
                pass
            if gemini_note == "gemini_fallback_used":
                try:
                    header["_worker_engine_used"] = "cloud_gemini"
                    header["_worker_cloud_gemini_authoritative"] = True
                except Exception:
                    pass
            job_all_text = "\n".join(
                str(x or "")
                for x in (
                    str(job.get("file_name") or ""),
                    job_text,
                    job_ocr_text,
                    job_pdf_text,
                )
                if str(x or "").strip()
            )
        if gemini_note == "gemini_fallback_used":
            override_note = "booker_purchase_table_skipped_cloud_gemini"
        else:
            header, items, metrics, override_note = _apply_worker_purchase_table_override(
                docmate_mod,
                settings,
                doc_type=doc_type,
                file_name=str(job.get("file_name") or ""),
                header=header,
                items=items,
                metrics=metrics,
                extracted_text=job_text,
                pdf_text=job_pdf_text,
                ocr_text=job_ocr_text,
                ocr_cache_key=job_ocr_cache_key,
            )
        if override_note:
            try:
                _sqlserver.log_event(f"Worker {override_note} file={job.get('file_name') or ''}")
            except Exception:
                pass
        parsed_results.append(
            {
                "file_name": str(job.get("file_name") or ""),
                "source_file_name": str(job.get("source_file_name") or source_file_name),
                "header": header,
                "items": items,
                "metrics": metrics,
                "extracted_text": job_all_text,
                "engine_used": "cloud_gemini" if gemini_note == "gemini_fallback_used" else "",
            }
        )
    timings["parse_s"] = time.perf_counter() - t_parse

    t_validate = time.perf_counter()
    for parsed in parsed_results:
        header = parsed["header"]
        items = parsed["items"]
        metrics = parsed["metrics"]
        extracted_text_current = parsed["extracted_text"]
        if endpoint_review:
            # Extraction-only destination: leave business validation to the receiver.
            # Keep the legacy argument name for existing callers.
            continue
        is_cloud_gemini = (
            str(parsed.get("engine_used") or "").strip() == "cloud_gemini"
            or str((header or {}).get("_worker_engine_used") or "").strip() == "cloud_gemini"
            or bool((header or {}).get("_worker_cloud_gemini_authoritative"))
        )
        try:
            if not is_cloud_gemini:
                header = _booker_worker_align_header_to_line_totals(docmate_mod, header, items, extracted_text_current)
                parsed["header"] = header
            try:
                recon_items_pre = docmate_mod._reconciliation_items(header, items, extracted_text_current)
            except Exception:
                recon_items_pre = items
            metrics = docmate_mod._calc_metrics(header, recon_items_pre)
            parsed["metrics"] = metrics
        except Exception:
            pass
        doc_number, doc_date = _derive_uploadedfile_doc_refs(
            docmate_mod,
            doc_type,
            header,
            str(parsed.get("file_name") or ""),
            fallback_text=extracted_text_current,
        )
        unsupported, unsupported_reason = _looks_unsupported_extraction(doc_type, header, items, metrics, extracted_text_current)
        if unsupported:
            timings["validate_s"] = time.perf_counter() - t_validate
            timings["total_s"] = time.perf_counter() - t0
            return False, unsupported_reason + _fmt_timings(timings), None, None, None, doc_number, doc_date, ISPROCESS_UNSUPPORTED
        vat_diff = abs(float(metrics.get("diff_vat", 0.0) or 0.0))
        if doc_type != "Expenses" and vat_diff > VAT_DIFF_EPS:
            try:
                is_booker_retry = bool(header.get("_worker_booker_detected") or header.get("_booker_header_override_used")) or bool(
                    docmate_mod._looks_like_booker_invoice(
                        "\n".join(
                            str(x or "")
                            for x in (
                                header.get("supplierName") if isinstance(header, dict) else "",
                                parsed.get("file_name"),
                                extracted_text_current,
                            )
                        )
                    )
                )
            except Exception:
                is_booker_retry = "BOOKER" in str(extracted_text_current or "").upper()
            if is_booker_retry and not is_cloud_gemini:
                retry_items = items
                try:
                    if hasattr(docmate_mod, "_booker_finalize_invoice_items"):
                        retry_items = docmate_mod._booker_finalize_invoice_items(retry_items, extracted_text_current)
                except Exception:
                    pass
                header = _booker_worker_align_header_to_line_totals(docmate_mod, header, retry_items, extracted_text_current)
                try:
                    retry_recon_items = docmate_mod._reconciliation_items(header, retry_items, extracted_text_current)
                except Exception:
                    retry_recon_items = retry_items
                try:
                    retry_metrics = docmate_mod._calc_metrics(header, retry_recon_items)
                    retry_vat_diff = abs(float(retry_metrics.get("diff_vat", 0.0) or 0.0))
                    if retry_vat_diff <= VAT_DIFF_EPS:
                        parsed["items"] = retry_items
                        parsed["metrics"] = retry_metrics
                        continue
                    vat_diff = retry_vat_diff
                    metrics = retry_metrics
                except Exception:
                    pass
            if vat_diff > VAT_DIFF_EPS:
                try:
                    line_vat = round(float(metrics.get("vat_items", metrics.get("sum_vat", 0.0)) or 0.0), 2)
                    line_gross = round(float(metrics.get("gross_items", metrics.get("sum_gross", 0.0)) or 0.0), 2)
                    line_net = round(line_gross - line_vat, 2) if line_gross > 0 and line_vat >= 0 else round(float(metrics.get("net_items", metrics.get("sum_net", 0.0)) or 0.0), 2)
                    hdr_vat = round(float(metrics.get("vat_header", 0.0) or 0.0), 2)
                    if (
                        doc_type == "Purchase Invoice"
                        and items
                        and line_vat > 0
                        and line_gross > 0
                        and abs(line_vat - hdr_vat) <= 25.00
                    ):
                        header = dict(header or {})
                        header["totalNet"] = line_net
                        header["totalVat"] = line_vat
                        header["totalGross"] = line_gross
                        parsed["header"] = header
                        try:
                            final_recon_items = docmate_mod._reconciliation_items(header, items, extracted_text_current)
                        except Exception:
                            final_recon_items = items
                        final_metrics = docmate_mod._calc_metrics(header, final_recon_items)
                        final_vat_diff = abs(float(final_metrics.get("diff_vat", 0.0) or 0.0))
                        if final_vat_diff <= VAT_DIFF_EPS:
                            parsed["metrics"] = final_metrics
                            continue
                        metrics = final_metrics
                        vat_diff = final_vat_diff
                except Exception:
                    pass
            timings["validate_s"] = time.perf_counter() - t_validate
            timings["total_s"] = time.perf_counter() - t0
            fee_present = any(
                "PICKING" in str((it or {}).get("description") or "").upper()
                or "DELIVERY" in str((it or {}).get("description") or "").upper()
                for it in (items or [])
                if isinstance(it, dict)
            )
            return (
                False,
                (
                    f"vat mismatch diff_vat={vat_diff:.2f} "
                    f"hdr_vat={float(metrics.get('vat_header', 0.0) or 0.0):.2f} "
                    f"line_vat={float(metrics.get('vat_items', 0.0) or 0.0):.2f} "
                    f"items={len(items or [])} fee_present={fee_present}"
                ) + _fmt_timings(timings),
                None,
                None,
                None,
                doc_number,
                doc_date,
                ISPROCESS_VAT_MISMATCH,
            )
    timings["validate_s"] = time.perf_counter() - t_validate

    t_save = time.perf_counter()
    saved_header_ids: list[int] = []
    saved_line_starts: list[int] = []
    saved_line_ends: list[int] = []
    saved_invoice_numbers: list[str] = []
    saved_invoice_dates: list[str] = []
    reused_invoices = 0
    for parsed in parsed_results:
        saved_reference: Dict[str, Any] = {}
        reference_args = {"saved_reference": saved_reference} if save_callback is _save_worker_result else {}
        ok_save, msg_save, header_id, line_id_start, line_id_end, doc_number, doc_date = save_callback(
            docmate_mod,
            doc_type=doc_type,
            header=parsed["header"],
            items=parsed["items"],
            metrics=parsed["metrics"],
            cfg_save=cfg_save,
            cms_customer_id=cms_customer_id,
            source_file_name=str(parsed.get("source_file_name") or source_file_name),
            ocr_file_name=str(parsed.get("file_name") or ocr_file_name),
            extracted_text=str(parsed.get("extracted_text") or ""),
            **reference_args,
        )
        if not ok_save:
            timings["save_s"] = time.perf_counter() - t_save
            timings["total_s"] = time.perf_counter() - t0
            return False, msg_save + _fmt_timings(timings), None, None, None, doc_number or None, doc_date or None, ISPROCESS_FAILED
        if str(msg_save).lower() == "duplicate skipped":
            reused_invoices += 1
        if header_id is not None:
            saved_header_ids.append(int(header_id))
        if saved_reference and invoice_references is not None:
            invoice_references.append(saved_reference)
        if line_id_start is not None:
            saved_line_starts.append(int(line_id_start))
        if line_id_end is not None:
            saved_line_ends.append(int(line_id_end))
        if doc_number:
            saved_invoice_numbers.append(str(doc_number))
        if doc_date:
            saved_invoice_dates.append(str(doc_date))

    timings["save_s"] = time.perf_counter() - t_save
    timings["total_s"] = time.perf_counter() - t0
    db_name = cfg_save.get("database") or ""
    total_lines = sum(len((parsed.get("items") or [])) for parsed in parsed_results)
    invoice_number_out = ", ".join(saved_invoice_numbers) if saved_invoice_numbers else None
    invoice_date_out = saved_invoice_dates[0] if saved_invoice_dates else None
    # A merged PDF has no single header or contiguous line range. Exact links
    # include reused invoices, whose IDs may predate this upload by months.
    single_invoice = len(parsed_results) == 1
    header_id_out = saved_header_ids[0] if single_invoice and saved_header_ids else None
    line_id_start_out = saved_line_starts[0] if single_invoice and saved_line_starts else None
    line_id_end_out = saved_line_ends[0] if single_invoice and saved_line_ends else None
    if save_callback is not _save_worker_result:
        header_id_out = min(saved_header_ids) if saved_header_ids else None
        line_id_start_out = min(saved_line_starts) if saved_line_starts else None
        line_id_end_out = max(saved_line_ends) if saved_line_ends else None
    multi_note = f" split_invoices={len(parsed_results)}" if len(parsed_results) > 1 else ""
    return True, (
        "ok"
        f" db={db_name}"
        f" invoices_processed={len(parsed_results)}"
        f" invoices_reused={reused_invoices}"
        f" extracted_lines={total_lines}"
        f" source={ocr_file_name}"
        f" doc={invoice_number_out or ''}/{invoice_date_out or ''}"
        f"{multi_note}"
        f"{_fmt_timings(timings)}"
    ), header_id_out, line_id_start_out, line_id_end_out, invoice_number_out, invoice_date_out, ISPROCESS_SUCCESS


def main() -> int:
    parser = argparse.ArgumentParser(description="Process UploadedFiles queue and run OCR.")
    parser.add_argument("--interval", type=int, default=10, help="Polling interval in seconds.")
    parser.add_argument("--once", action="store_true", help="Run once then exit.")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Load standalone DocMate APIs only; do not connect to or update the queue.",
    )
    args = parser.parse_args()
    if args.smoke_test:
        ok, message = smoke_test_load()
        print(("OK: " if ok else "ERROR: ") + message)
        return 0 if ok else 2
    if args.once:
        print(f"DocMate worker build: {WORKER_BUILD}")

    try:
        docmate_mod = _load_docmate_module()
        docmate_mtime_ns = _docmate_source_mtime_ns()
        try:
            _sqlserver.log_event(
                f"DocMate worker started build={WORKER_BUILD} module={DOCMATE_PATH.name} "
                f"python={sys.executable} gemini_sdk[{_worker_gemini_sdk_status()}]"
            )
        except Exception:
            pass
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _sqlserver.log_event(f"Worker failed to load DocMate module: {e}\n{tb}")
        print(f"ERROR: {e}\n{tb}")
        return 2

    cfg_save = _sqlserver._load_sqlserver_config()
    if not cfg_save:
        _sqlserver.log_event("db_cred.enc not found or empty; worker cannot start.")
        print("ERROR: db_cred.enc not found or empty")
        return 2
    cfg_queue = dict(cfg_save)
    cfg_queue["database"] = "Docmate"

    while True:
        try:
            conn = _sqlserver._connect(cfg_queue)
        except Exception as e:
            _sqlserver.log_event(f"SQL Server connection failed: {e}")
            time.sleep(max(args.interval, 5))
            if args.once:
                return 2
            continue

        try:
            _ensure_uploadedfiles_tracking_columns(conn)
            row = _fetch_next_uploaded(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        if not row:
            if args.once:
                print("No pending UploadedFiles rows found (IsProcess = 0).")
                return 0
            time.sleep(args.interval)
            continue

        row_id = row.get("Id")
        file_path = row.get("FilePath") or ""
        file_name = row.get("FileName") or os.path.basename(file_path)
        process_type = row.get("ProcessType") or "Purchase Invoice"
        cms_id = _parse_cms_id_from_filename(file_name)
        if args.once:
            print(f"Processing UploadedFiles Id={row_id} file={file_name} type={process_type}")

        try:
            docmate_mod, docmate_mtime_ns = _reload_docmate_module_if_changed(docmate_mod, docmate_mtime_ns)
        except Exception as e:
            _sqlserver.log_event(f"Worker failed to reload updated DocMate module; using previous version: {e}")

        invoice_references: list[Dict[str, Any]] = []
        try:
            ok, msg, header_id, line_id_start, line_id_end, invoice_number, invoice_date, status = _process_file(
                docmate_mod, str(file_path), cms_id, str(process_type), cfg_save=cfg_save,
                invoice_references=invoice_references,
            )
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            ok = False
            msg = f"worker exception while processing file: {e}"
            header_id = None
            line_id_start = None
            line_id_end = None
            invoice_number = None
            invoice_date = None
            status = ISPROCESS_FAILED
            _sqlserver.log_event(f"Worker exception Id={row_id} file={file_name}: {msg}\n{tb}")

        conn2 = None
        tracking_ok = False
        try:
            conn2 = _sqlserver._connect(cfg_queue)
            _ensure_uploadedfiles_tracking_columns(conn2)
            if ok:
                _update_uploadedfile_tracking(conn2, row_id, status, header_id, line_id_start, line_id_end, invoice_number, invoice_date, invoice_references)
            else:
                _update_uploadedfile_tracking(conn2, row_id, status, None, None, None, invoice_number, invoice_date, invoice_references)
            tracking_ok = True
        except Exception as e:
            if conn2 is not None:
                conn2.rollback()
            _sqlserver.log_event(f"Failed to update IsProcess for Id={row_id}: {e}")
        finally:
            if conn2 is not None:
                conn2.close()

        if not tracking_ok:
            if args.once:
                return 2
            continue

        if not ok:
            copied = _copy_to_unprocessed(str(file_path))
            if not copied:
                _sqlserver.log_event(f"Failed to copy file to unprocessed: {file_path}")
            _sqlserver.log_event(f"Worker failed Id={row_id} file={file_name}: {msg}")
            if args.once:
                print(f"FAILED status={status} Id={row_id} file={file_name}: {msg}")
        else:
            _sqlserver.log_event(f"Worker ok Id={row_id} file={file_name}: {msg}")
            if args.once:
                print(
                    f"OK status={status} Id={row_id} file={file_name} "
                    f"header_id={header_id} lines={line_id_start}-{line_id_end}: {msg}"
                )

        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())

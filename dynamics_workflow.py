"""Standalone Dynamics attachment OCR queue worker.

This worker deliberately shares only the v1.18 OCR/parsing pipeline with the
convenience worker. Queue reads, result writes, statuses, and configuration are
Dynamics-specific.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pyodbc

if __package__:
    from . import worker_uploadedfiles as ocr_worker
else:
    import worker_uploadedfiles as ocr_worker


BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BASE_DIR.parent))
from docmate_core.printed_invoice import printed_payload, printed_records
from docmate_core.pdf_capture import capture_document
from docmate_core.gemini_capture import gemini_capture, gemini_settings, should_run
CONFIG_PATH = BASE_DIR / "dynamics_db.yaml"

TASK_TABLE = "dbo.OCR_TaskEntry"
HEADER_TABLE = "dbo.OCR_InvoiceHeader"
LINE_TABLE = "dbo.OCR_InvoiceLine"

DEFAULT_INTERVAL_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("dynamics_workflow")


def _read_config(path: Path = CONFIG_PATH) -> Dict[str, str]:
    if not path.exists():
        raise FileNotFoundError(f"Dynamics configuration not found: {path}")
    config: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        config[key.strip()] = value.strip().strip('"').strip("'")
    return config


def _connect(config: Dict[str, str]) -> pyodbc.Connection:
    driver = config.get("driver") or "ODBC Driver 17 for SQL Server"
    server = config.get("server") or "."
    database = config.get("database") or "OcrDynamics"
    username = config.get("username") or ""
    password = config.get("password") or ""
    timeout = int(config.get("timeout") or 10)

    if username:
        auth = f"UID={username};PWD={password};"
    else:
        auth = "Trusted_Connection=yes;"
    connection_string = (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};{auth}"
        "Encrypt=no;TrustServerCertificate=yes;"
    )
    return pyodbc.connect(connection_string, timeout=timeout, autocommit=False)


def _row_dict(cursor, row) -> Dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def _ensure_dynamics_result_tables(connection: pyodbc.Connection) -> None:
    """Create/upgrade Dynamics OCR tables using the convenience data model."""
    sql = f"""
    IF OBJECT_ID('{HEADER_TABLE}', 'U') IS NULL
    BEGIN
        CREATE TABLE {HEADER_TABLE} (
            InvoiceHeaderId INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            TaskEntryId BIGINT NOT NULL,
            TaskId INT NULL,
            created_utc NVARCHAR(64) NOT NULL,
            filename NVARCHAR(260) NULL,
            supplier_name NVARCHAR(260) NULL,
            customer_name NVARCHAR(260) NULL,
            doc_number NVARCHAR(64) NULL,
            doc_date NVARCHAR(64) NULL,
            order_number NVARCHAR(128) NULL,
            account_number NVARCHAR(128) NULL,
            customer_number NVARCHAR(128) NULL,
            delivery_note_number NVARCHAR(128) NULL,
            due_date NVARCHAR(64) NULL,
            payment_terms NVARCHAR(260) NULL,
            subtotal FLOAT NULL,
            discount_amount FLOAT NULL,
            shipping_amount FLOAT NULL,
            vat_registration_number NVARCHAR(128) NULL,
            total_ex_vat FLOAT NULL,
            total_vat FLOAT NULL,
            total_inc_vat FLOAT NULL,
            currency NVARCHAR(16) NULL,
            raw_header_json NVARCHAR(MAX) NULL,
            additional_fields_json NVARCHAR(MAX) NULL,
            CombinedJson NVARCHAR(MAX) NULL,
            PushedToBC BIT NOT NULL DEFAULT(0),
            PushedAt DATETIME2 NULL
        );
    END;
    IF COL_LENGTH('{HEADER_TABLE}', 'TaskEntryId') IS NULL ALTER TABLE {HEADER_TABLE} ADD TaskEntryId BIGINT NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'TaskId') IS NULL ALTER TABLE {HEADER_TABLE} ADD TaskId INT NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'SupplierName') IS NULL ALTER TABLE {HEADER_TABLE} ADD SupplierName NVARCHAR(260) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'InvoiceNumber') IS NULL ALTER TABLE {HEADER_TABLE} ADD InvoiceNumber NVARCHAR(64) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'InvoiceDate') IS NULL ALTER TABLE {HEADER_TABLE} ADD InvoiceDate DATE NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'TotalAmount') IS NULL ALTER TABLE {HEADER_TABLE} ADD TotalAmount DECIMAL(18,2) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'created_utc') IS NULL ALTER TABLE {HEADER_TABLE} ADD created_utc NVARCHAR(64) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'filename') IS NULL ALTER TABLE {HEADER_TABLE} ADD filename NVARCHAR(260) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'supplier_name') IS NULL ALTER TABLE {HEADER_TABLE} ADD supplier_name NVARCHAR(260) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'customer_name') IS NULL ALTER TABLE {HEADER_TABLE} ADD customer_name NVARCHAR(260) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'doc_number') IS NULL ALTER TABLE {HEADER_TABLE} ADD doc_number NVARCHAR(64) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'doc_date') IS NULL ALTER TABLE {HEADER_TABLE} ADD doc_date NVARCHAR(64) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'order_number') IS NULL ALTER TABLE {HEADER_TABLE} ADD order_number NVARCHAR(128) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'account_number') IS NULL ALTER TABLE {HEADER_TABLE} ADD account_number NVARCHAR(128) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'customer_number') IS NULL ALTER TABLE {HEADER_TABLE} ADD customer_number NVARCHAR(128) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'delivery_note_number') IS NULL ALTER TABLE {HEADER_TABLE} ADD delivery_note_number NVARCHAR(128) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'due_date') IS NULL ALTER TABLE {HEADER_TABLE} ADD due_date NVARCHAR(64) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'payment_terms') IS NULL ALTER TABLE {HEADER_TABLE} ADD payment_terms NVARCHAR(260) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'subtotal') IS NULL ALTER TABLE {HEADER_TABLE} ADD subtotal FLOAT NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'discount_amount') IS NULL ALTER TABLE {HEADER_TABLE} ADD discount_amount FLOAT NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'shipping_amount') IS NULL ALTER TABLE {HEADER_TABLE} ADD shipping_amount FLOAT NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'vat_registration_number') IS NULL ALTER TABLE {HEADER_TABLE} ADD vat_registration_number NVARCHAR(128) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'total_ex_vat') IS NULL ALTER TABLE {HEADER_TABLE} ADD total_ex_vat FLOAT NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'total_vat') IS NULL ALTER TABLE {HEADER_TABLE} ADD total_vat FLOAT NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'total_inc_vat') IS NULL ALTER TABLE {HEADER_TABLE} ADD total_inc_vat FLOAT NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'raw_header_json') IS NULL ALTER TABLE {HEADER_TABLE} ADD raw_header_json NVARCHAR(MAX) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'additional_fields_json') IS NULL ALTER TABLE {HEADER_TABLE} ADD additional_fields_json NVARCHAR(MAX) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'CombinedJson') IS NULL ALTER TABLE {HEADER_TABLE} ADD CombinedJson NVARCHAR(MAX) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'MappingReady') IS NULL ALTER TABLE {HEADER_TABLE} ADD MappingReady BIT NOT NULL DEFAULT(0);
    IF COL_LENGTH('{HEADER_TABLE}', 'MappingErrorsJson') IS NULL ALTER TABLE {HEADER_TABLE} ADD MappingErrorsJson NVARCHAR(MAX) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'MappingAuditJson') IS NULL ALTER TABLE {HEADER_TABLE} ADD MappingAuditJson NVARCHAR(MAX) NULL;
    IF COL_LENGTH('{HEADER_TABLE}', 'doc_type') IS NOT NULL ALTER TABLE {HEADER_TABLE} DROP COLUMN doc_type;
    IF COL_LENGTH('{HEADER_TABLE}', 'invoice_fingerprint') IS NOT NULL ALTER TABLE {HEADER_TABLE} DROP COLUMN invoice_fingerprint;
    IF COL_LENGTH('{HEADER_TABLE}', 'Compared') IS NOT NULL ALTER TABLE {HEADER_TABLE} DROP COLUMN Compared;
    IF COL_LENGTH('{HEADER_TABLE}', 'Generated') IS NOT NULL ALTER TABLE {HEADER_TABLE} DROP COLUMN Generated;
    IF COL_LENGTH('{HEADER_TABLE}', 'StartSync') IS NOT NULL ALTER TABLE {HEADER_TABLE} DROP COLUMN StartSync;
    IF COL_LENGTH('{HEADER_TABLE}', 'EndSync') IS NOT NULL ALTER TABLE {HEADER_TABLE} DROP COLUMN EndSync;

    IF OBJECT_ID('{LINE_TABLE}', 'U') IS NULL
    BEGIN
        CREATE TABLE {LINE_TABLE} (
            InvoiceLineId INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
            InvoiceHeaderId INT NOT NULL,
            TaskEntryId BIGINT NOT NULL,
            TaskId INT NULL,
            created_utc NVARCHAR(64) NOT NULL,
            filename NVARCHAR(260) NULL,
            supplier_name NVARCHAR(260) NULL,
            customer_name NVARCHAR(260) NULL,
            doc_number NVARCHAR(64) NULL,
            doc_date NVARCHAR(64) NULL,
            line_no INT NULL,
            LineNumber INT NULL,
            code NVARCHAR(64) NULL,
            barcode NVARCHAR(128) NULL,
            description NVARCHAR(512) NULL,
            case_pack FLOAT NULL,
            unit_size NVARCHAR(64) NULL,
            total_units FLOAT NULL,
            qty FLOAT NULL,
            unit_price FLOAT NULL,
            discount_percent FLOAT NULL,
            discount_amount FLOAT NULL,
            line_net FLOAT NULL,
            vat_code NVARCHAR(16) NULL,
            vat_rate FLOAT NULL,
            vat_amount FLOAT NULL,
            line_gross FLOAT NULL,
            raw_line_json NVARCHAR(MAX) NULL
            ,additional_fields_json NVARCHAR(MAX) NULL
        );
    END;
    IF COL_LENGTH('{LINE_TABLE}', 'InvoiceHeaderId') IS NULL ALTER TABLE {LINE_TABLE} ADD InvoiceHeaderId INT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'TaskEntryId') IS NULL ALTER TABLE {LINE_TABLE} ADD TaskEntryId BIGINT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'TaskId') IS NULL ALTER TABLE {LINE_TABLE} ADD TaskId INT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'created_utc') IS NULL ALTER TABLE {LINE_TABLE} ADD created_utc NVARCHAR(64) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'filename') IS NULL ALTER TABLE {LINE_TABLE} ADD filename NVARCHAR(260) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'supplier_name') IS NULL ALTER TABLE {LINE_TABLE} ADD supplier_name NVARCHAR(260) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'customer_name') IS NULL ALTER TABLE {LINE_TABLE} ADD customer_name NVARCHAR(260) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'doc_number') IS NULL ALTER TABLE {LINE_TABLE} ADD doc_number NVARCHAR(64) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'doc_date') IS NULL ALTER TABLE {LINE_TABLE} ADD doc_date NVARCHAR(64) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'line_no') IS NULL ALTER TABLE {LINE_TABLE} ADD line_no INT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'LineNumber') IS NULL ALTER TABLE {LINE_TABLE} ADD LineNumber INT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'code') IS NULL ALTER TABLE {LINE_TABLE} ADD code NVARCHAR(64) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'barcode') IS NULL ALTER TABLE {LINE_TABLE} ADD barcode NVARCHAR(128) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'description') IS NULL ALTER TABLE {LINE_TABLE} ADD description NVARCHAR(512) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'case_pack') IS NULL ALTER TABLE {LINE_TABLE} ADD case_pack FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'unit_size') IS NULL ALTER TABLE {LINE_TABLE} ADD unit_size NVARCHAR(64) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'total_units') IS NULL ALTER TABLE {LINE_TABLE} ADD total_units FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'qty') IS NULL ALTER TABLE {LINE_TABLE} ADD qty FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'unit_price') IS NULL ALTER TABLE {LINE_TABLE} ADD unit_price FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'discount_percent') IS NULL ALTER TABLE {LINE_TABLE} ADD discount_percent FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'discount_amount') IS NULL ALTER TABLE {LINE_TABLE} ADD discount_amount FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'line_net') IS NULL ALTER TABLE {LINE_TABLE} ADD line_net FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'vat_code') IS NULL ALTER TABLE {LINE_TABLE} ADD vat_code NVARCHAR(16) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'vat_rate') IS NULL ALTER TABLE {LINE_TABLE} ADD vat_rate FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'vat_amount') IS NULL ALTER TABLE {LINE_TABLE} ADD vat_amount FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'line_gross') IS NULL ALTER TABLE {LINE_TABLE} ADD line_gross FLOAT NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'raw_line_json') IS NULL ALTER TABLE {LINE_TABLE} ADD raw_line_json NVARCHAR(MAX) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'additional_fields_json') IS NULL ALTER TABLE {LINE_TABLE} ADD additional_fields_json NVARCHAR(MAX) NULL;
    IF COL_LENGTH('{LINE_TABLE}', 'doc_type') IS NOT NULL ALTER TABLE {LINE_TABLE} DROP COLUMN doc_type;
    IF COL_LENGTH('{LINE_TABLE}', 'std_rrp') IS NOT NULL ALTER TABLE {LINE_TABLE} DROP COLUMN std_rrp;
    IF COL_LENGTH('{LINE_TABLE}', 'por') IS NOT NULL ALTER TABLE {LINE_TABLE} DROP COLUMN por;
    IF COL_LENGTH('{LINE_TABLE}', 'pmp') IS NOT NULL ALTER TABLE {LINE_TABLE} DROP COLUMN pmp;
    IF COL_LENGTH('{LINE_TABLE}', 'is_void') IS NOT NULL ALTER TABLE {LINE_TABLE} DROP COLUMN is_void;
    IF COL_LENGTH('{LINE_TABLE}', 'void_note') IS NOT NULL ALTER TABLE {LINE_TABLE} DROP COLUMN void_note;
    IF COL_LENGTH('{LINE_TABLE}', 'raw') IS NOT NULL ALTER TABLE {LINE_TABLE} DROP COLUMN raw;
    IF COL_LENGTH('{LINE_TABLE}', 'invoice_fingerprint') IS NOT NULL ALTER TABLE {LINE_TABLE} DROP COLUMN invoice_fingerprint;
    """
    connection.cursor().execute(sql)
    connection.commit()


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        cleaned = str(value).strip().replace(",", "")
        for symbol in ("£", "$", "€"):
            cleaned = cleaned.replace(symbol, "")
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _optional_number(value):
    if value in (None, ""):
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('Invoice amount must be finite.')
    return result


def _line_number(value: Any, fallback: int) -> int:
    try:
        number = int(_decimal(value))
        return number if number > 0 else fallback
    except (TypeError, ValueError, OverflowError):
        return fallback


def _date_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, date_format).date().isoformat()
        except ValueError:
            pass
    return text


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _first(mapping: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return default


def _additional_fields(mapping: Dict[str, Any], known_keys: set[str]) -> Dict[str, Any]:
    """Preserve non-empty OCR fields that are not part of the stable contract."""
    return {
        str(key): value
        for key, value in mapping.items()
        if key not in known_keys
        and not str(key).startswith("_")
        and value not in (None, "", [], {})
    }


def _field_review(model, mapped):
    """Describe extra tags and review candidates without logging their values."""
    def tags(extra, used):
        # `used` is tag -> BC field actually sent, after supplier rules and discount resolution.
        return [
            {"tag": tag, "outgoingFields": [used[tag]] if tag in used else []}
            for tag in sorted(extra)
        ]

    tag_map = mapped['bc_tables']['tagMap']
    header_tags = tags(model['invoiceHeader']['additionalFields'], tag_map['header'])
    line_tags = [
        {"lineNumber": line['lineNumber'],
         "tags": tags(line['additionalFields'], used)}
        for line, used in zip(model['invoiceLines'], tag_map['lines'])
        if line['additionalFields']
    ]
    all_tags = header_tags + [tag for line in line_tags for tag in line['tags']]
    return {
        "headerTags": header_tags,
        "lineTags": line_tags,
        "unmappedTags": sorted({tag['tag'] for tag in all_tags if not tag['outgoingFields']}),
        "extractionReviewReasons": mapped['validation']['errors'],
        "configurationReviewReasons": mapped['validation']['masterDataWarnings'],
        "supplierRulesApplied": mapped['bc_tables']['supplierRulesApplied'],
    }


def _claim_next_task(connection: pyodbc.Connection) -> Optional[Dict[str, Any]]:
    """Claim a pending attachment; failed tasks require an explicit reset to Pending."""
    sql = f"""
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    BEGIN TRANSACTION;
    ;WITH next_task AS (
        SELECT TOP (1) *
        FROM {TASK_TABLE} WITH (READPAST, UPDLOCK, ROWLOCK)
        WHERE IsAttachment = 1
          AND ISNULL(IsProcessing, 0) = 0
          AND ISNULL(IsComplete, 0) = 0
          AND SavedFilePath IS NOT NULL
          AND LTRIM(RTRIM(SavedFilePath)) <> ''
          AND ISNULL(Status, 'Pending') = 'Pending'
        ORDER BY TaskEntryId
    )
    UPDATE next_task
       SET IsProcessing = 1, Status = 'Processing'
    OUTPUT inserted.*;
    COMMIT TRANSACTION;
    """
    cursor = connection.cursor()
    rows = cursor.execute(sql).fetchall()
    connection.commit()
    return _row_dict(cursor, rows[0]) if rows else None


def _set_task_status(
    config: Dict[str, str],
    task_entry_id: Any,
    *,
    is_processing: int,
    is_complete: int,
    status: str,
) -> None:
    connection = _connect(config)
    try:
        connection.cursor().execute(
            f"""UPDATE {TASK_TABLE}
                SET IsProcessing=?, IsComplete=?, Status=?
                WHERE TaskEntryId=?""",
            is_processing,
            is_complete,
            status,
            task_entry_id,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _capture_pdf_data(source_path: str, dynamics_config: Dict[str, str]) -> Dict[str, Any]:
    """Everything printed on the document (fields, tables, text). Never fails the invoice."""
    try:
        use_ocr = str(dynamics_config.get('pdf_capture_ocr', 'yes')).strip().lower() not in ('no', 'false', '0')
        return capture_document(source_path, use_ocr=use_ocr)
    except Exception as exc:
        logger.warning('PDF capture failed for %s: %s', source_path, exc)
        return {'error': f'PDF capture failed: {exc}'}


def _gemini_pdf_data(docmate_mod, doc_type: str, header: Dict[str, Any], items: list, extracted_text: str,
                     local_capture: Dict[str, Any], source_path: str,
                     dynamics_config: Dict[str, str]) -> Dict[str, Any]:
    """Gemini fallback for the whole document: Gemini reads every page and returns all printed data.
    Runs when the local result is weak (low confidence, extractor already used Gemini, scanned
    document, few fields found) or always if gemini_capture: always. Never fails the invoice."""
    cfg = gemini_settings(dynamics_config)
    score, threshold = None, 65
    try:
        settings = docmate_mod._read_settings() if hasattr(docmate_mod, '_read_settings') else {}
        threshold = int(settings.get('auto_confidence_review_threshold', 65) or 65)
        if hasattr(docmate_mod, '_assess_extraction_confidence'):
            confidence = docmate_mod._assess_extraction_confidence(
                header or {}, items or [], doc_type, raw_text=extracted_text or '',
                engine_used=str((header or {}).get('_worker_engine_used') or 'worker_local'))
            score = int((confidence or {}).get('score') or 0)
    except Exception as exc:
        logger.warning('Confidence check for Gemini capture failed: %s', exc)
    used_gemini = (str((header or {}).get('_worker_engine_used') or '') == 'cloud_gemini'
                   or bool((header or {}).get('_worker_cloud_gemini_authoritative'))
                   or bool((header or {}).get('_cloud_fallback_reason')))
    run, reason = should_run(cfg['mode'], confidence_score=score, threshold=threshold,
                             extractor_used_gemini=used_gemini, local_capture=local_capture)
    info = {'ran': False, 'reason': reason, 'confidenceScore': score, 'threshold': threshold}
    if not run:
        return info
    if not cfg['api_key']:
        return {**info, 'error': 'No Gemini API key: set GOOGLE_API_KEY in .env'}
    try:
        result = gemini_capture(source_path, cfg['api_key'], cfg['model'])
        logger.info('Gemini capture ran (%s) with %s: %s fields, %s tables', reason, result.get('model'),
                    len(result.get('fields') or {}), len(result.get('tables') or []))
        return {**info, 'ran': True, **result}
    except Exception as exc:
        logger.warning('Gemini capture failed for %s: %s', source_path, exc)
        return {**info, 'error': f'Gemini capture failed: {exc}'}


def _log_stage(task_entry_id, stage, data):
    """Correlate full invoice diagnostics without logging configuration or credentials."""
    logger.info("Dynamics diagnostic TaskEntryId=%s stage=%s:\n%s",
                task_entry_id, stage,
                json.dumps(data, ensure_ascii=False, default=_json_default, indent=2))


def _make_save_callback(task_entry_id: Any, task_id: Any, dynamics_config: Dict[str, str], source_path: str = ''):
    """Build the Dynamics destination callback expected by _process_file."""
    pdf_data_cache: Dict[str, Any] = {}

    def _save(
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
    ) -> Tuple[bool, str, Optional[int], Optional[int], Optional[int], Optional[str], Optional[str]]:
        del cfg_save, cms_customer_id, ocr_file_name

        original_header = dict(header)
        _log_stage(task_entry_id, 'extraction_result', {
            'sourcePath': source_path, 'documentType': doc_type,
            'engine': header.get('_worker_engine_used', 'local_or_unmarked'),
            'cloudAuthoritative': bool(header.get('_worker_cloud_gemini_authoritative')),
            'header': header, 'items': items, 'reconciliation': metrics,
            'extractedText': extracted_text,
            'note': 'Parser output may contain inferred or calculated values; internal diagnostics only.',
        })
        # Captured once per file (a file can hold several invoices).
        if source_path and 'data' not in pdf_data_cache:
            capture_started = time.perf_counter()
            _log_stage(task_entry_id, 'capture_started', {'sourcePath': source_path})
            pdf_data_cache['data'] = _capture_pdf_data(source_path, dynamics_config)
            pdf_data_cache['data']['gemini'] = _gemini_pdf_data(
                docmate_mod, doc_type, header, list(items or []), extracted_text,
                pdf_data_cache['data'], source_path, {**dynamics_config, 'gemini_capture': 'always'})
        pdf_data = pdf_data_cache.get('data') or {}
        gemini = pdf_data.get('gemini') or {}
        _log_stage(task_entry_id, 'document_capture', {
            'captureSeconds': round(time.perf_counter() - capture_started, 3) if 'capture_started' in locals() else None,
            'geminiRan': gemini.get('ran'), 'geminiModel': gemini.get('model'),
            'captureError': pdf_data.get('error'), 'geminiError': gemini.get('error'),
            'data': pdf_data,
        })
        original_items = list(items or [])
        # Dynamics is an extraction destination: do not run accounting reconciliation,
        # supplier tax rules, enrichment, or calculated-value overlays here.
        header, items = printed_records(gemini)
        capture_errors = [] if header and items else ['printed_capture_incomplete']
        mapped = {'validation': {'readyForDelivery': not capture_errors,
                                 'errors': capture_errors}}

        supplier = str(header.get("supplierName") or "").strip()
        customer = str(header.get("customerName") or "").strip()
        invoice_number = str(header.get("invoiceNumber") or "").strip()
        invoice_date = _date_text(header.get("invoiceDate"))
        order_number = str(_first(header, "orderNumber", "purchaseOrderNumber", "poNumber", "orderNo", default="")).strip()
        account_number = str(_first(header, "accountNumber", "accountNo", default="")).strip()
        customer_number = str(_first(header, "customerNumber", "customerNo", default="")).strip()
        delivery_note_number = str(_first(header, "deliveryNoteNumber", "deliveryNoteNo", default="")).strip()
        due_date = _date_text(_first(header, "dueDate", "paymentDueDate"))
        payment_terms = str(_first(header, "paymentTerms", "paymentDueText", default="")).strip()
        currency = str(header.get("currency") or "").strip()
        subtotal = _decimal(_first(header, "subtotal", "subTotal", "totalBeforeDiscount"))
        header_discount = _decimal(_first(header, "discountAmount", "totalDiscount"))
        shipping_amount = _decimal(_first(header, "shippingAmount", "deliveryCharge", "freightAmount"))
        vat_registration = str(_first(header, "supplierVatNo", "vatRegistrationNumber", "vatNumber", "supplierVatNumber", default="")).strip()
        total_net = _optional_number(header.get("totalNet"))
        total_vat = _optional_number(header.get("totalVat"))
        total_gross = _optional_number(header.get("totalGross"))
        created_utc = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        raw_header_json = json.dumps(original_header, ensure_ascii=False, default=_json_default)
        header_known = {
            "supplierName", "customerName", "invoiceNumber", "invoiceDate", "currency",
            "totalNet", "totalVat", "totalGross", "totalAmount", "subtotal", "subTotal",
            "totalBeforeDiscount", "discountAmount", "totalDiscount", "shippingAmount",
            "deliveryCharge", "freightAmount", "orderNumber", "purchaseOrderNumber",
            "poNumber", "orderNo", "accountNumber", "accountNo", "customerNumber",
            "customerNo", "deliveryNoteNumber", "deliveryNoteNo", "dueDate",
            "paymentDueDate", "paymentTerms", "paymentDueText", "vatRegistrationNumber",
            "vatNumber", "supplierVatNumber", "supplierVatNo",
        }
        header_additional = _additional_fields(header, header_known)
        header_additional_json = json.dumps(header_additional, ensure_ascii=False, default=_json_default)

        model: Dict[str, Any] = {
            "taskId": task_id,
            "taskEntryId": task_entry_id,
            "mappingVersion": 2,
            "mappingTarget": "BC Purchase Header 38 / Purchase Line 39",
            "mappingValidation": mapped['validation'],
            "invoiceHeader": {
                "supplierName": supplier,
                "invoiceNumber": invoice_number,
                "invoiceDate": invoice_date,
                "orderNumber": order_number,
                "accountNumber": account_number,
                "customerNumber": customer_number,
                "deliveryNoteNumber": delivery_note_number,
                "dueDate": due_date,
                "paymentTerms": payment_terms,
                "currency": currency,
                "subtotal": subtotal,
                "discountAmount": header_discount,
                "shippingAmount": shipping_amount,
                "totalNet": total_net,
                "totalVat": total_vat,
                "totalGross": total_gross,
                "vatRegistrationNumber": vat_registration,
                "additionalFields": header_additional,
            },
            "invoiceLines": [],
        }

        connection = _connect(dynamics_config)
        try:
            cursor = connection.cursor()
            existing = cursor.execute(
                f"SELECT TOP (1) InvoiceHeaderId, PushedToBC FROM {HEADER_TABLE} WITH (UPDLOCK, HOLDLOCK) WHERE TaskEntryId=?",
                task_entry_id,
            ).fetchone()

            if existing:
                if existing[1]:
                    raise ValueError('This invoice was already pushed to BC; it cannot be overwritten.')
                header_id = int(existing[0])
                cursor.execute(
                    f"""UPDATE {HEADER_TABLE}
                        SET TaskId=?, created_utc=?, filename=?,
                            supplier_name=?, customer_name=?, doc_number=?, doc_date=?,
                            order_number=?, account_number=?, customer_number=?, delivery_note_number=?,
                            due_date=?, payment_terms=?, subtotal=?, discount_amount=?, shipping_amount=?,
                            vat_registration_number=?,
                            total_ex_vat=?, total_vat=?, total_inc_vat=?, currency=?,
                            raw_header_json=?, additional_fields_json=?, PushedToBC=0, PushedAt=NULL,
                            SupplierName=?, InvoiceNumber=?, InvoiceDate=?, TotalAmount=?
                        WHERE InvoiceHeaderId=?""",
                    task_id,
                    created_utc,
                    source_file_name,
                    supplier or None,
                    customer or None,
                    invoice_number or None,
                    invoice_date,
                    order_number or None, account_number or None, customer_number or None,
                    delivery_note_number or None, due_date, payment_terms or None,
                    float(subtotal), float(header_discount), float(shipping_amount), vat_registration or None,
                    total_net,
                    total_vat,
                    total_gross,
                    currency,
                    raw_header_json,
                    header_additional_json,
                    supplier or None, invoice_number or None, invoice_date, total_gross,
                    header_id,
                )
                cursor.execute(f"DELETE FROM {LINE_TABLE} WHERE InvoiceHeaderId=?", header_id)
            else:
                row = cursor.execute(
                    f"""INSERT INTO {HEADER_TABLE}
                        (TaskEntryId, TaskId, created_utc, filename,
                         supplier_name, customer_name, doc_number, doc_date,
                         order_number, account_number, customer_number, delivery_note_number,
                         due_date, payment_terms, subtotal, discount_amount, shipping_amount,
                         vat_registration_number,
                         total_ex_vat, total_vat, total_inc_vat, currency,
                         raw_header_json, additional_fields_json, PushedToBC, PushedAt,
                         SupplierName, InvoiceNumber, InvoiceDate, TotalAmount)
                        OUTPUT inserted.InvoiceHeaderId
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,?,?,?,?)""",
                    task_entry_id,
                    task_id,
                    created_utc,
                    source_file_name,
                    supplier or None,
                    customer or None,
                    invoice_number or None,
                    invoice_date,
                    order_number or None, account_number or None, customer_number or None,
                    delivery_note_number or None, due_date, payment_terms or None,
                    float(subtotal), float(header_discount), float(shipping_amount), vat_registration or None,
                    total_net,
                    total_vat,
                    total_gross,
                    currency,
                    raw_header_json,
                    header_additional_json,
                    supplier or None, invoice_number or None, invoice_date, total_gross,
                ).fetchone()
                header_id = int(row[0])

            first_line_id: Optional[int] = None
            last_line_id: Optional[int] = None
            for index, item in enumerate(items or [], start=1):
                number = _line_number(item.get("lineNo") or item.get("lineNumber"), index)
                code = str(item.get("code") or "").strip()
                barcode = str(_first(item, "barcode", "ean", "gtin", default="")).strip()
                description = str(item.get("description") or "").strip()
                case_pack = _decimal(item.get("casePack"))
                unit_size = str(item.get("unitSize") or "").strip()
                quantity = _optional_number(item.get("qty"))
                total_units = _decimal(item.get("totalUnits"))
                unit_price = _optional_number(item.get("unitPrice"))
                discount_percent = _decimal(_first(item, "discountPercent", "discountRate", "discountPct"))
                line_discount = _decimal(_first(item, "discountAmount", "lineDiscount"))
                line_net = _optional_number(item.get("lineNet"))
                vat_code = str(item.get("vatCode") or "").strip()
                vat_rate = _optional_number(item.get("vatRate"))
                vat_amount = _optional_number(item.get("vatAmount"))
                line_gross = _optional_number(item.get("lineGross"))
                raw_line_json = json.dumps(original_items[index - 1] if index <= len(original_items) else {}, ensure_ascii=False, default=_json_default)
                line_known = {
                    "lineNo", "lineNumber", "code", "barcode", "ean", "gtin", "description",
                    "casePack", "unitSize", "totalUnits", "qty", "quantity", "unitPrice",
                    "price", "discountPercent", "discountRate", "discountPct", "discountAmount",
                    "lineDiscount", "lineNet", "lineAmount", "vatCode", "vatRate", "vatAmount",
                    "lineGross",
                }
                line_additional = _additional_fields(item, line_known)
                line_additional_json = json.dumps(line_additional, ensure_ascii=False, default=_json_default)

                model["invoiceLines"].append(
                    {
                        "lineNumber": number,
                        "code": code,
                        "barcode": barcode,
                        "description": description,
                        "casePack": case_pack,
                        "unitSize": unit_size,
                        "totalUnits": total_units,
                        "quantity": quantity,
                        "unitPrice": unit_price,
                        "discountPercent": discount_percent,
                        "discountAmount": line_discount,
                        "lineNet": line_net,
                        "vatCode": vat_code,
                        "vatRate": vat_rate,
                        "vatAmount": vat_amount,
                        "lineGross": line_gross,
                        "additionalFields": line_additional,
                    }
                )
                line_row = cursor.execute(
                    f"""INSERT INTO {LINE_TABLE}
                        (InvoiceHeaderId, TaskEntryId, TaskId, created_utc, filename,
                         supplier_name, customer_name, doc_number, doc_date,
                         line_no, LineNumber, code, barcode, description, case_pack, unit_size, total_units,
                         qty, unit_price, discount_percent, discount_amount, line_net,
                         vat_code, vat_rate, vat_amount, line_gross, raw_line_json, additional_fields_json)
                        OUTPUT inserted.InvoiceLineId
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    header_id,
                    task_entry_id,
                    task_id,
                    created_utc,
                    source_file_name,
                    supplier or None,
                    customer or None,
                    invoice_number or None,
                    invoice_date,
                    number,
                    number,
                    code or None,
                    barcode or None,
                    description or None,
                    float(case_pack), unit_size or None, float(total_units), quantity,
                    unit_price, float(discount_percent), float(line_discount), line_net,
                    vat_code or None, vat_rate, vat_amount, line_gross,
                    raw_line_json, line_additional_json,
                ).fetchone()
                line_id = int(line_row[0])
                first_line_id = line_id if first_line_id is None else first_line_id
                last_line_id = line_id

            mapping_audit = {}
            printed_header, printed_lines = printed_payload(gemini, mapping_audit=mapping_audit)
            _log_stage(task_entry_id, 'bc_mapping_coverage', mapping_audit)
            if mapping_audit['unmappedHeaderTags'] or mapping_audit['unmappedLineTags']:
                logger.warning(
                    'BC mapping missing TaskEntryId=%s taskId=%s headerTags=%s lineTags=%s; '
                    'fields sent under original tags',
                    task_entry_id, task_id, mapping_audit['unmappedHeaderTags'],
                    mapping_audit['unmappedLineTags'])
            model['bcMappingCoverage'] = mapping_audit
            printed = gemini.get('printedInvoice') or {}
            evidence = printed.get('evidence') or {}
            def omissions(values, quotes):
                return [{'field': key, 'value': value,
                         'reason': 'internal_field' if key.startswith('_') else
                                   'missing_source_quote'}
                        for key, value in values.items()
                        if key.startswith('_') or not isinstance(quotes.get(key), str)
                        or not quotes[key].strip()]
            quotes = evidence.get('items') or []
            _log_stage(task_entry_id, 'printed_field_mapping', {
                'source': printed, 'outgoingHeader': printed_header,
                'outgoingLines': printed_lines,
                'omittedHeaderFields': omissions(printed.get('header') or {}, evidence.get('header') or {}),
                'omittedLineFields': [
                    {'row': i + 1, 'fields': omissions(row, quotes[i] if i < len(quotes) else {})}
                    for i, row in enumerate(printed.get('items') or [])],
            })
            outgoing = {
                'taskId': task_id,
                'invoiceHeader': printed_header,
                'invoiceLines': printed_lines,
                'mappingCoverage': mapping_audit,
                'documentCapture': {key: gemini.get(key) or default for key, default in
                                    (('fields', {}), ('addresses', []), ('tables', []), ('notes', []))},
            }
            # Full printed capture is also delivered so unmapped labels and address blocks survive.
            model['documentCapture'] = pdf_data
            model['printedCaptureComplete'] = bool(printed_header and printed_lines)
            model['sourceInvoice'] = {'invoiceHeader': model['invoiceHeader'],
                                      'invoiceLines': model['invoiceLines']}
            del model['invoiceHeader'], model['invoiceLines']
            model['mappingVersion'] = 4
            mapping_audit_json = json.dumps(model, ensure_ascii=False,
                                            default=_json_default, allow_nan=False)
            combined_json = json.dumps(
                outgoing,
                ensure_ascii=False,
                default=_json_default,
                allow_nan=False,
            )
            cursor.execute(
                f"UPDATE {HEADER_TABLE} SET CombinedJson=?, MappingReady=?, MappingErrorsJson=?, MappingAuditJson=? WHERE InvoiceHeaderId=?",
                combined_json,
                int(mapped['validation']['readyForDelivery']),
                json.dumps(mapped['validation']['errors']),
                mapping_audit_json,
                header_id,
            )
            connection.commit()
            _log_stage(task_entry_id, 'payload_saved', {
                'invoiceHeaderId': header_id, 'payloadBytes': len(combined_json.encode('utf-8')),
                'lineCount': len(printed_lines),
                'mappingReady': mapped['validation']['readyForDelivery'],
                'note': 'CombinedJson saved to SQL; this is not confirmation of delivery to BC.',
            })
            logger.info(
                "Dynamics extracted JSON TaskEntryId=%s:\n%s",
                task_entry_id,
                combined_json,
            )
            message = "Dynamics header and mapped JSON saved"
            if not mapped['validation']['readyForDelivery']:
                message += "; capture incomplete: " + '; '.join(mapped['validation']['errors'])
            return True, message, header_id, first_line_id, last_line_id, invoice_number or None, invoice_date
        except Exception as exc:
            connection.rollback()
            logger.exception("Dynamics save failed TaskEntryId=%s", task_entry_id)
            return False, f"Dynamics SQL save failed: {exc}", None, None, None, invoice_number or None, invoice_date
        finally:
            connection.close()

    return _save


def process_next(config: Optional[Dict[str, str]] = None) -> bool:
    """Claim and process one record; return False when the queue is empty."""
    config = config or _read_config()
    connection = _connect(config)
    try:
        _ensure_dynamics_result_tables(connection)
        task = _claim_next_task(connection)
    finally:
        connection.close()
    if not task:
        return False

    task_entry_id = task.get("TaskEntryId")
    task_id = task.get("TaskId")
    file_path = str(task.get("SavedFilePath") or "").strip()
    logger.info("Claimed Dynamics TaskEntryId=%s path=%s", task_entry_id, file_path)

    try:
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"SavedFilePath does not exist: {file_path}")
        docmate_module = ocr_worker._load_docmate_module()
        result = ocr_worker._process_file(
            docmate_module,
            file_path=file_path,
            cms_customer_id="",
            process_type="Purchase Invoice",
            cfg_save=config,
            save_callback=_make_save_callback(task_entry_id, task_id, config, file_path),
            endpoint_review=True,
        )
        if not bool(result[0]):
            raise RuntimeError(str(result[1] or "Dynamics OCR failed"))
        _set_task_status(config, task_entry_id, is_processing=0, is_complete=1, status="Completed")
        logger.info("Completed Dynamics TaskEntryId=%s: %s", task_entry_id, result[1])
    except Exception as exc:
        logger.exception("Failed Dynamics TaskEntryId=%s: %s", task_entry_id, exc)
        _set_task_status(config, task_entry_id, is_processing=0, is_complete=0, status="Failed")
    return True


def run_once() -> bool:
    return process_next(_read_config())


def run_forever(interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> None:
    config = _read_config()
    logger.info("Dynamics worker started database=%s", config.get("database") or "OcrDynamics")
    while True:
        try:
            if not process_next(config):
                time.sleep(max(1, interval_seconds))
        except KeyboardInterrupt:
            logger.info("Dynamics worker stopped")
            return
        except Exception:
            logger.exception("Dynamics polling error")
            time.sleep(max(5, interval_seconds))


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone DocMate Dynamics OCR worker")
    parser.add_argument("--once", action="store_true", help="Process at most one pending task")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()
    if args.once:
        if not run_once():
            logger.info("No pending Dynamics OCR task found")
        return 0
    run_forever(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

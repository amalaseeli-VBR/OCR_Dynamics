from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from pathlib import Path
from datetime import datetime
import os
import json
import math


# Local fallback logger (avoids dependency on missing eventlog module)
def log_event(message: str) -> None:
    try:
        os.makedirs("DocMate_DATA", exist_ok=True)
        ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        log_path = os.path.join("DocMate_DATA", "EventLOG_" + datetime.now().strftime("%Y-%m-%d") + ".txt")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


DEFAULT_TABLE = "DocMate_invoice_headers"
DEFAULT_LINES_TABLE = "DocMate_invoice_line_Data"
DEFAULT_EXPENSES_HEADER_TABLE = "Expenses_header"
DEFAULT_CUSTOMER_PROFILE_TABLE = "Customer_profile"


def _json_safe_model(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe_model(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe_model(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe_model(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json_model_text(value: Any) -> str:
    try:
        return json.dumps(_json_safe_model(value or {}), ensure_ascii=False, default=str, sort_keys=True, allow_nan=False)
    except Exception:
        try:
            return json.dumps({"value": str(value)}, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except Exception:
            return "{}"


def _parse_yaml_simple(text: str) -> Dict[str, str]:
    """
    Minimal YAML parser for simple 'key: value' lines.
    Avoids adding a dependency for pyyaml.
    """
    data: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        data[key] = val
    return data


def _read_yaml_simple(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    return _parse_yaml_simple(path.read_text(encoding="utf-8", errors="ignore"))


def _load_sqlserver_config() -> Dict[str, str]:
    # Look for customer_db_cred.yaml in repo root
    root = Path(__file__).resolve().parent
    cfg_path = root / "customer_db_cred.yaml"
    cfg = _read_yaml_simple(cfg_path)
    return cfg or {}


def _build_conn_str(cfg: Dict[str, str]) -> str:
    server = (cfg.get("server", ".") or ".").strip()
    port = (cfg.get("port") or "").strip()
    if port and ("," not in server) and (":" not in server):
        server = f"{server},{port}"
    database = cfg.get("database", "")
    username = cfg.get("username", "")
    password = cfg.get("password", "")
    driver = cfg.get("driver", "ODBC Driver 17 for SQL Server")
    encrypt = (cfg.get("encrypt") or "no").strip().lower()
    trust_cert = (cfg.get("trust_server_certificate") or "yes").strip().lower()
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        f"Encrypt={encrypt};"
        f"TrustServerCertificate={trust_cert};"
    )


def _connect(cfg: Dict[str, str]):
    import pyodbc
    conn_str = _build_conn_str(cfg)
    return pyodbc.connect(conn_str, timeout=10)


def ensure_table(conn, table: str = DEFAULT_TABLE) -> None:
    sql = f"""
    IF NOT EXISTS (
        SELECT 1
        FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        WHERE t.name = '{table}' AND s.name = 'dbo'
    )
    BEGIN
        CREATE TABLE dbo.{table} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            created_utc NVARCHAR(64) NOT NULL,
            filename NVARCHAR(260) NULL,
            doc_type NVARCHAR(64) NULL,
            supplier_name NVARCHAR(260) NULL,
            customer_name NVARCHAR(260) NULL,
            cms_customer_id NVARCHAR(64) NULL,
            doc_number NVARCHAR(64) NULL,
            doc_date NVARCHAR(64) NULL,
            total_ex_vat FLOAT NULL,
            total_vat FLOAT NULL,
            total_inc_vat FLOAT NULL,
            currency NVARCHAR(16) NULL,
            raw_header_json NVARCHAR(MAX) NULL,
            Compared BIT NULL,
            Generated BIT NULL,
            StartSync BIT NULL,
            EndSync BIT NULL
        );
    END
    IF EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'UX_invoice_headers_natural'
               AND object_id = OBJECT_ID('dbo.{table}'))
        DROP INDEX UX_invoice_headers_natural ON dbo.{table};
    IF COL_LENGTH('dbo.{table}', 'invoice_fingerprint') IS NULL
        ALTER TABLE dbo.{table} ADD invoice_fingerprint NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'cms_customer_id') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD cms_customer_id NVARCHAR(64) NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'raw_header_json') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD raw_header_json NVARCHAR(MAX) NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'Compared') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD Compared BIT NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'Generated') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD Generated BIT NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'StartSync') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD StartSync BIT NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'EndSync') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD EndSync BIT NULL;
    END
    """
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()


def ensure_expenses_header_table(conn, table: str = DEFAULT_EXPENSES_HEADER_TABLE) -> None:
    sql = f"""
    IF NOT EXISTS (
        SELECT 1
        FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        WHERE t.name = '{table}' AND s.name = 'dbo'
    )
    BEGIN
        CREATE TABLE dbo.{table} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            created_utc NVARCHAR(64) NOT NULL,
            filename NVARCHAR(260) NULL,
            source_attachment NVARCHAR(260) NULL,
            doc_type NVARCHAR(64) NULL,
            supplier_name NVARCHAR(260) NULL,
            customer_name NVARCHAR(260) NULL,
            cms_customer_id NVARCHAR(64) NULL,
            doc_number NVARCHAR(64) NULL,
            doc_date NVARCHAR(64) NULL,
            total_ex_vat FLOAT NULL,
            total_vat FLOAT NULL,
            total_inc_vat FLOAT NULL,
            currency NVARCHAR(16) NULL,
            account_number NVARCHAR(128) NULL,
            contract_number NVARCHAR(128) NULL,
            customer_no NVARCHAR(128) NULL,
            site_address NVARCHAR(MAX) NULL,
            bill_to_address NVARCHAR(MAX) NULL,
            service_period NVARCHAR(260) NULL,
            payment_due_date NVARCHAR(64) NULL,
            payment_due_text NVARCHAR(512) NULL,
            utility_type NVARCHAR(128) NULL,
            meter_number NVARCHAR(128) NULL,
            mpan NVARCHAR(128) NULL,
            mprn NVARCHAR(128) NULL,
            vat_registration_number NVARCHAR(128) NULL,
            extraction_mode NVARCHAR(128) NULL,
            raw_header_json NVARCHAR(MAX) NULL,
            Compared BIT NULL,
            Generated BIT NULL,
            StartSync BIT NULL,
            EndSync BIT NULL
        );
    END
    IF NOT EXISTS (
        SELECT 1
        FROM sys.indexes
        WHERE name = 'UX_expenses_header_natural'
          AND object_id = OBJECT_ID('dbo.{table}')
    )
    BEGIN
        CREATE UNIQUE INDEX UX_expenses_header_natural
        ON dbo.{table} (supplier_name, doc_number, doc_date);
    END
    IF COL_LENGTH('dbo.{table}', 'source_attachment') IS NULL ALTER TABLE dbo.{table} ADD source_attachment NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'cms_customer_id') IS NULL ALTER TABLE dbo.{table} ADD cms_customer_id NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'account_number') IS NULL ALTER TABLE dbo.{table} ADD account_number NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'contract_number') IS NULL ALTER TABLE dbo.{table} ADD contract_number NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'customer_no') IS NULL ALTER TABLE dbo.{table} ADD customer_no NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'site_address') IS NULL ALTER TABLE dbo.{table} ADD site_address NVARCHAR(MAX) NULL;
    IF COL_LENGTH('dbo.{table}', 'bill_to_address') IS NULL ALTER TABLE dbo.{table} ADD bill_to_address NVARCHAR(MAX) NULL;
    IF COL_LENGTH('dbo.{table}', 'service_period') IS NULL ALTER TABLE dbo.{table} ADD service_period NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'payment_due_date') IS NULL ALTER TABLE dbo.{table} ADD payment_due_date NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'payment_due_text') IS NULL ALTER TABLE dbo.{table} ADD payment_due_text NVARCHAR(512) NULL;
    IF COL_LENGTH('dbo.{table}', 'utility_type') IS NULL ALTER TABLE dbo.{table} ADD utility_type NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'meter_number') IS NULL ALTER TABLE dbo.{table} ADD meter_number NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'mpan') IS NULL ALTER TABLE dbo.{table} ADD mpan NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'mprn') IS NULL ALTER TABLE dbo.{table} ADD mprn NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'vat_registration_number') IS NULL ALTER TABLE dbo.{table} ADD vat_registration_number NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'extraction_mode') IS NULL ALTER TABLE dbo.{table} ADD extraction_mode NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'raw_header_json') IS NULL ALTER TABLE dbo.{table} ADD raw_header_json NVARCHAR(MAX) NULL;
    IF COL_LENGTH('dbo.{table}', 'Compared') IS NULL ALTER TABLE dbo.{table} ADD Compared BIT NULL;
    IF COL_LENGTH('dbo.{table}', 'Generated') IS NULL ALTER TABLE dbo.{table} ADD Generated BIT NULL;
    IF COL_LENGTH('dbo.{table}', 'StartSync') IS NULL ALTER TABLE dbo.{table} ADD StartSync BIT NULL;
    IF COL_LENGTH('dbo.{table}', 'EndSync') IS NULL ALTER TABLE dbo.{table} ADD EndSync BIT NULL;
    """
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()


def ensure_lines_table(conn, table: str = DEFAULT_LINES_TABLE) -> None:
    sql = f"""
    IF NOT EXISTS (
        SELECT 1
        FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        WHERE t.name = '{table}' AND s.name = 'dbo'
    )
    BEGIN
        CREATE TABLE dbo.{table} (
            id INT IDENTITY(1,1) PRIMARY KEY,
            created_utc NVARCHAR(64) NOT NULL,
            filename NVARCHAR(260) NULL,
            doc_type NVARCHAR(64) NULL,
            supplier_name NVARCHAR(260) NULL,
            customer_name NVARCHAR(260) NULL,
            cms_customer_id NVARCHAR(64) NULL,
            doc_number NVARCHAR(64) NULL,
            doc_date NVARCHAR(64) NULL,
            line_no INT NULL,
            code NVARCHAR(64) NULL,
            description NVARCHAR(512) NULL,
            case_pack FLOAT NULL,
            unit_size NVARCHAR(64) NULL,
            total_units FLOAT NULL,
            qty FLOAT NULL,
            unit_price FLOAT NULL,
            line_net FLOAT NULL,
            vat_code NVARCHAR(16) NULL,
            vat_rate FLOAT NULL,
            vat_amount FLOAT NULL,
            line_gross FLOAT NULL,
            std_rrp FLOAT NULL,
            por FLOAT NULL,
            is_void INT NULL,
            void_note NVARCHAR(512) NULL,
            pmp FLOAT NULL,
            raw NVARCHAR(MAX) NULL,
            raw_line_json NVARCHAR(MAX) NULL
        );
    END
    IF NOT EXISTS (
        SELECT 1
        FROM sys.indexes
        WHERE name = 'IX_docmate_lines_doc'
          AND object_id = OBJECT_ID('dbo.{table}')
    )
    BEGIN
        CREATE INDEX IX_docmate_lines_doc
        ON dbo.{table} (doc_type, supplier_name, doc_number, doc_date);
    END
    IF COL_LENGTH('dbo.{table}', 'cms_customer_id') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD cms_customer_id NVARCHAR(64) NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'total_units') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD total_units FLOAT NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'is_void') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD is_void INT NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'void_note') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD void_note NVARCHAR(512) NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'pmp') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD pmp FLOAT NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'raw_line_json') IS NULL
    BEGIN
        ALTER TABLE dbo.{table} ADD raw_line_json NVARCHAR(MAX) NULL;
    END
    IF COL_LENGTH('dbo.{table}', 'invoice_fingerprint') IS NULL
        ALTER TABLE dbo.{table} ADD invoice_fingerprint NVARCHAR(64) NULL;
    """
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()


def ensure_customer_profiles_table(conn, table: str = DEFAULT_CUSTOMER_PROFILE_TABLE) -> None:
    sql = f"""
    IF NOT EXISTS (
        SELECT 1
        FROM sys.tables t
        JOIN sys.schemas s ON t.schema_id = s.schema_id
        WHERE t.name = '{table}' AND s.name = 'dbo'
    )
    BEGIN
        CREATE TABLE dbo.{table} (
            cms_customer_id NVARCHAR(64) NOT NULL PRIMARY KEY,
            mobile_cms_id NVARCHAR(64) NULL,
            store_id NVARCHAR(64) NULL,
            customer_name NVARCHAR(260) NULL,
            spos_customer_id NVARCHAR(64) NULL,
            trading_name NVARCHAR(260) NULL,
            company_name NVARCHAR(260) NULL,
            vat_registration_no NVARCHAR(64) NULL,
            contact_no NVARCHAR(64) NULL,
            email_address NVARCHAR(260) NULL,
            contact_name NVARCHAR(260) NULL,
            contact_email NVARCHAR(260) NULL,
            contact_phone NVARCHAR(64) NULL,
            address_line1 NVARCHAR(260) NULL,
            address_line2 NVARCHAR(260) NULL,
            city NVARCHAR(128) NULL,
            postcode NVARCHAR(32) NULL,
            country NVARCHAR(128) NULL,
            address NVARCHAR(512) NULL,
            license_start_date NVARCHAR(64) NULL,
            license_end_date NVARCHAR(64) NULL,
            notes NVARCHAR(512) NULL,
            created_at NVARCHAR(64) NULL,
            updated_at NVARCHAR(64) NULL
        );
    END
    IF COL_LENGTH('dbo.{table}', 'mobile_cms_id') IS NULL ALTER TABLE dbo.{table} ADD mobile_cms_id NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'store_id') IS NULL ALTER TABLE dbo.{table} ADD store_id NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'customer_name') IS NULL ALTER TABLE dbo.{table} ADD customer_name NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'spos_customer_id') IS NULL ALTER TABLE dbo.{table} ADD spos_customer_id NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'trading_name') IS NULL ALTER TABLE dbo.{table} ADD trading_name NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'company_name') IS NULL ALTER TABLE dbo.{table} ADD company_name NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'vat_registration_no') IS NULL ALTER TABLE dbo.{table} ADD vat_registration_no NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'contact_no') IS NULL ALTER TABLE dbo.{table} ADD contact_no NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'email_address') IS NULL ALTER TABLE dbo.{table} ADD email_address NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'contact_name') IS NULL ALTER TABLE dbo.{table} ADD contact_name NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'contact_email') IS NULL ALTER TABLE dbo.{table} ADD contact_email NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'contact_phone') IS NULL ALTER TABLE dbo.{table} ADD contact_phone NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'address_line1') IS NULL ALTER TABLE dbo.{table} ADD address_line1 NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'address_line2') IS NULL ALTER TABLE dbo.{table} ADD address_line2 NVARCHAR(260) NULL;
    IF COL_LENGTH('dbo.{table}', 'city') IS NULL ALTER TABLE dbo.{table} ADD city NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'postcode') IS NULL ALTER TABLE dbo.{table} ADD postcode NVARCHAR(32) NULL;
    IF COL_LENGTH('dbo.{table}', 'country') IS NULL ALTER TABLE dbo.{table} ADD country NVARCHAR(128) NULL;
    IF COL_LENGTH('dbo.{table}', 'address') IS NULL ALTER TABLE dbo.{table} ADD address NVARCHAR(512) NULL;
    IF COL_LENGTH('dbo.{table}', 'license_start_date') IS NULL ALTER TABLE dbo.{table} ADD license_start_date NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'license_end_date') IS NULL ALTER TABLE dbo.{table} ADD license_end_date NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'notes') IS NULL ALTER TABLE dbo.{table} ADD notes NVARCHAR(512) NULL;
    IF COL_LENGTH('dbo.{table}', 'created_at') IS NULL ALTER TABLE dbo.{table} ADD created_at NVARCHAR(64) NULL;
    IF COL_LENGTH('dbo.{table}', 'updated_at') IS NULL ALTER TABLE dbo.{table} ADD updated_at NVARCHAR(64) NULL;
    """
    cur = conn.cursor()
    cur.execute(sql)
    conn.commit()


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None
        s = s.replace(",", "").replace("%", "")
        return float(s)
    except Exception:
        return None


def insert_invoice_lines_sqlserver(
    header: Dict[str, Any],
    items: list[Dict[str, Any]],
    table: str = DEFAULT_LINES_TABLE,
    cfg_override: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    cfg = cfg_override or _load_sqlserver_config()
    if not cfg:
        log_event("SQL Server config missing (customer_db_cred.yaml not found or empty)")
        return False, "customer_db_cred.yaml not found or empty"
    try:
        conn = _connect(cfg)
    except Exception as e:
        log_event(f"SQL Server connection failed: {e}")
        return False, f"SQL Server connection failed: {e}"

    def _g(it: Dict[str, Any], *keys, default=None):
        for k in keys:
            if k in it and it.get(k) not in (None, ""):
                return it.get(k)
        return default

    def _vat_rate_from_code(vc: Any) -> Optional[float]:
        code = (str(vc).strip().upper() if vc is not None else "")
        if code in ("B", "S", "S/A", "SA"):
            return 20.0
        if code in ("R", "R5"):
            return 5.0
        if code in ("A", "Z", "Z/A", "ZA", "E", "X"):
            return 0.0
        return None

    try:
        ensure_lines_table(conn, table=table)
        cur = conn.cursor()

        doc_type = header.get("doc_type")
        supplier_name = header.get("supplier_name")
        doc_number = header.get("doc_number")
        doc_date = header.get("doc_date")
        fingerprint = header.get("invoice_fingerprint")
        if fingerprint:
            cur.execute(f"SELECT TOP 1 1 FROM dbo.{table} WHERE invoice_fingerprint=?", (fingerprint,))
            if cur.fetchone():
                return True, "duplicate"

        rows = []
        for idx, it in enumerate(items or [], start=1):
            line_no = _to_float(_g(it, "lineNo", "line_no", "LINE NO"))
            if line_no is not None:
                try:
                    line_no = int(line_no)
                except Exception:
                    pass
            if line_no is None:
                line_no = idx
            case_pack = _to_float(_g(it, "casePack", "case_pack", "PACK"))
            unit_size = _g(it, "unitSize", "unit_size", "SIZE")

            vat_code = _g(it, "vatCode", "vat_code", "VAT")
            vat_rate = _to_float(_g(it, "vatRate", "vat_rate"))
            if vat_rate is None or vat_rate == 0:
                vr = _vat_rate_from_code(vat_code)
                if vr is not None:
                    vat_rate = vr
            line_net = _to_float(_g(it, "lineNet", "line_net", "VALUE"))
            vat_amount = _to_float(_g(it, "vatAmount", "vat_amount"))
            if (vat_amount is None or vat_amount == 0) and line_net and vat_rate:
                vat_amount = round(line_net * (vat_rate / 100.0), 2)
            line_gross = _to_float(_g(it, "lineGross", "line_gross"))
            if (line_gross is None or line_gross == 0) and line_net is not None:
                line_gross = round(line_net + (vat_amount or 0.0), 2)
            code = _g(it, "code", "CODE")
            description = _g(it, "description", "DESCRIPTION")
            qty = _to_float(_g(it, "qty", "QTY"))
            total_units = _to_float(_g(it, "totalUnits", "total_units"))
            if (total_units is None or total_units == 0) and case_pack and qty:
                total_units = float(case_pack) * float(qty)
            unit_price = _to_float(_g(it, "unitPrice", "unit_price", "PRICE"))
            std_rrp = _to_float(_g(it, "stdRrp", "std_rrp", "STD RRP"))
            por = _to_float(_g(it, "por", "POR"))
            is_void = _g(it, "isVoid", "is_void", "IS VOID")
            try:
                is_void = int(is_void) if is_void not in (None, "") else None
            except Exception:
                is_void = None
            void_note = _g(it, "voidNote", "void_note", "VOID NOTE")
            pmp = _to_float(_g(it, "pmp", "PMP"))
            raw = _g(it, "raw", "RAW")
            raw_line_json = _g(it, "raw_line_json", "line_json", "LINE_JSON")
            if raw_line_json is None:
                raw_line_json = _json_model_text(it)

            rows.append(
                (
                    header.get("created_utc"),
                    header.get("filename"),
                    doc_type,
                    supplier_name,
                    header.get("customer_name"),
                    header.get("cms_customer_id"),
                    doc_number,
                    doc_date,
                    line_no,
                    code,
                    description,
                    case_pack,
                    unit_size,
                    total_units,
                    qty,
                    unit_price,
                    line_net,
                    vat_code,
                    vat_rate,
                    vat_amount,
                    line_gross,
                    std_rrp,
                    por,
                    is_void,
                    void_note,
                    pmp,
                    raw,
                    raw_line_json,
                    header.get("invoice_fingerprint"),
                )
            )

        if rows:
            cur.executemany(
                f"""
                INSERT INTO dbo.{table} (
                    created_utc, filename, doc_type, supplier_name, customer_name, cms_customer_id, doc_number, doc_date,
                    line_no, code, description, case_pack, unit_size, total_units, qty, unit_price, line_net,
                    vat_code, vat_rate, vat_amount, line_gross, std_rrp, por, is_void, void_note, pmp, raw, raw_line_json,
                    invoice_fingerprint
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
        conn.commit()
        log_event(f"SQL Server line items insert ok into dbo.{table} (rows={len(rows)})")
        return True, f"ok ({len(rows)} rows)"
    except Exception as e:
        log_event(f"SQL line items insert failed: {e}")
        return False, f"SQL line items insert failed: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def insert_invoice_header_sqlserver(
    row: Dict[str, Any],
    table: str = DEFAULT_TABLE,
    cfg_override: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    cfg = cfg_override or _load_sqlserver_config()
    if not cfg:
        log_event("SQL Server config missing (customer_db_cred.yaml not found or empty)")
        return False, "customer_db_cred.yaml not found or empty"
    try:
        conn = _connect(cfg)
    except Exception as e:
        log_event(f"SQL Server connection failed: {e}")
        return False, f"SQL Server connection failed: {e}"
    try:
        ensure_table(conn, table=table)
        ensure_lines_table(conn, table=DEFAULT_LINES_TABLE)
        cur = conn.cursor()
        raw_header_json = row.get("raw_header_json")
        if raw_header_json is None and isinstance(row.get("raw_header"), dict):
            raw_header_json = _json_model_text(row.get("raw_header"))
        if raw_header_json is None:
            raw_header_json = _json_model_text(row)
        fingerprint = row.get("invoice_fingerprint")
        if fingerprint:
            cur.execute(f"SELECT TOP 1 1 FROM dbo.{table} WHERE invoice_fingerprint=?", (fingerprint,))
            if cur.fetchone():
                cur.execute(
                    f"SELECT TOP 1 1 FROM dbo.{DEFAULT_LINES_TABLE} WHERE invoice_fingerprint=?",
                    (fingerprint,),
                )
                return (True, "duplicate") if cur.fetchone() else (True, "resume_lines")
        cur.execute(
            f"""INSERT INTO dbo.{table} (
                created_utc, filename, doc_type, supplier_name, customer_name, cms_customer_id, doc_number, doc_date,
                total_ex_vat, total_vat, total_inc_vat, currency, raw_header_json, invoice_fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                row.get("created_utc"),
                row.get("filename"),
                row.get("doc_type"),
                row.get("supplier_name"),
                row.get("customer_name"),
                row.get("cms_customer_id"),
                row.get("doc_number"),
                row.get("doc_date"),
                row.get("total_ex_vat"),
                row.get("total_vat"),
                row.get("total_inc_vat"),
                row.get("currency"),
                raw_header_json,
                fingerprint,
            ),
        )
        conn.commit()
        log_event(f"SQL Server insert ok into dbo.{table}")
        return True, "ok"
    except Exception as e:
        log_event(f"SQL insert failed: {e}")
        return False, f"SQL insert failed: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def insert_expenses_header_sqlserver(
    row: Dict[str, Any],
    table: str = DEFAULT_EXPENSES_HEADER_TABLE,
    cfg_override: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    cfg = cfg_override or _load_sqlserver_config()
    if not cfg:
        log_event("SQL Server config missing (customer_db_cred.yaml not found or empty)")
        return False, "customer_db_cred.yaml not found or empty"
    try:
        conn = _connect(cfg)
    except Exception as e:
        log_event(f"SQL Server connection failed: {e}")
        return False, f"SQL Server connection failed: {e}"
    try:
        ensure_expenses_header_table(conn, table=table)
        raw_header = row.get("raw_header_json")
        if raw_header is None and isinstance(row.get("raw_header"), dict):
            raw_header = json.dumps(row.get("raw_header") or {}, ensure_ascii=False)
        cur = conn.cursor()
        cur.execute(
            f"""
            MERGE dbo.{table} AS target
            USING (VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)) AS src(
                created_utc, filename, source_attachment, doc_type, supplier_name, customer_name,
                cms_customer_id, doc_number, doc_date, total_ex_vat, total_vat, total_inc_vat,
                currency, account_number, contract_number, customer_no, site_address, bill_to_address,
                service_period, payment_due_date, payment_due_text, utility_type, meter_number,
                mpan, mprn, vat_registration_number, extraction_mode, raw_header_json,
                Compared, Generated, StartSync
            )
            ON target.supplier_name = src.supplier_name
               AND target.doc_number = src.doc_number
               AND target.doc_date = src.doc_date
            WHEN MATCHED THEN
                UPDATE SET
                    created_utc = src.created_utc,
                    filename = src.filename,
                    source_attachment = src.source_attachment,
                    doc_type = src.doc_type,
                    customer_name = src.customer_name,
                    cms_customer_id = src.cms_customer_id,
                    total_ex_vat = src.total_ex_vat,
                    total_vat = src.total_vat,
                    total_inc_vat = src.total_inc_vat,
                    currency = src.currency,
                    account_number = src.account_number,
                    contract_number = src.contract_number,
                    customer_no = src.customer_no,
                    site_address = src.site_address,
                    bill_to_address = src.bill_to_address,
                    service_period = src.service_period,
                    payment_due_date = src.payment_due_date,
                    payment_due_text = src.payment_due_text,
                    utility_type = src.utility_type,
                    meter_number = src.meter_number,
                    mpan = src.mpan,
                    mprn = src.mprn,
                    vat_registration_number = src.vat_registration_number,
                    extraction_mode = src.extraction_mode,
                    raw_header_json = src.raw_header_json
            WHEN NOT MATCHED THEN
                INSERT (
                    created_utc, filename, source_attachment, doc_type, supplier_name, customer_name,
                    cms_customer_id, doc_number, doc_date, total_ex_vat, total_vat, total_inc_vat,
                    currency, account_number, contract_number, customer_no, site_address, bill_to_address,
                    service_period, payment_due_date, payment_due_text, utility_type, meter_number,
                    mpan, mprn, vat_registration_number, extraction_mode, raw_header_json,
                    Compared, Generated, StartSync
                )
                VALUES (
                    src.created_utc, src.filename, src.source_attachment, src.doc_type, src.supplier_name, src.customer_name,
                    src.cms_customer_id, src.doc_number, src.doc_date, src.total_ex_vat, src.total_vat, src.total_inc_vat,
                    src.currency, src.account_number, src.contract_number, src.customer_no, src.site_address, src.bill_to_address,
                    src.service_period, src.payment_due_date, src.payment_due_text, src.utility_type, src.meter_number,
                    src.mpan, src.mprn, src.vat_registration_number, src.extraction_mode, src.raw_header_json,
                    src.Compared, src.Generated, src.StartSync
                );
            """,
            (
                row.get("created_utc"),
                row.get("filename"),
                row.get("source_attachment"),
                row.get("doc_type"),
                row.get("supplier_name"),
                row.get("customer_name"),
                row.get("cms_customer_id"),
                row.get("doc_number"),
                row.get("doc_date"),
                row.get("total_ex_vat"),
                row.get("total_vat"),
                row.get("total_inc_vat"),
                row.get("currency"),
                row.get("account_number"),
                row.get("contract_number"),
                row.get("customer_no"),
                row.get("site_address"),
                row.get("bill_to_address"),
                row.get("service_period"),
                row.get("payment_due_date"),
                row.get("payment_due_text"),
                row.get("utility_type"),
                row.get("meter_number"),
                row.get("mpan"),
                row.get("mprn"),
                row.get("vat_registration_number"),
                row.get("extraction_mode"),
                raw_header,
                row.get("Compared"),
                row.get("Generated"),
                row.get("StartSync"),
            ),
        )
        conn.commit()
        log_event(f"SQL Server expenses header upsert ok into dbo.{table}")
        return True, "ok"
    except Exception as e:
        log_event(f"SQL expenses header upsert failed: {e}")
        return False, f"SQL expenses header upsert failed: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def insert_customer_profile_sqlserver(
    row: Dict[str, Any],
    table: str = DEFAULT_CUSTOMER_PROFILE_TABLE,
    cfg_override: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    cfg = cfg_override or _load_sqlserver_config()
    if not cfg:
        log_event("SQL Server config missing (customer_db_cred.yaml not found or empty)")
        return False, "customer_db_cred.yaml not found or empty"
    try:
        conn = _connect(cfg)
    except Exception as e:
        log_event(f"SQL Server connection failed: {e}")
        return False, f"SQL Server connection failed: {e}"
    try:
        ensure_customer_profiles_table(conn, table=table)
        cur = conn.cursor()
        cur.execute(
            f"""
            MERGE dbo.{table} AS target
            USING (VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)) AS src(
                cms_customer_id, mobile_cms_id, store_id, customer_name, spos_customer_id,
                trading_name, company_name, vat_registration_no, contact_no, email_address,
                contact_name, contact_email, contact_phone, address_line1, address_line2,
                city, postcode, country, address, license_start_date, license_end_date,
                notes, created_at, updated_at
            )
            ON target.cms_customer_id = src.cms_customer_id
            WHEN MATCHED THEN
                UPDATE SET
                    mobile_cms_id = src.mobile_cms_id,
                    store_id = src.store_id,
                    customer_name = src.customer_name,
                    spos_customer_id = src.spos_customer_id,
                    trading_name = src.trading_name,
                    company_name = src.company_name,
                    vat_registration_no = src.vat_registration_no,
                    contact_no = src.contact_no,
                    email_address = src.email_address,
                    contact_name = src.contact_name,
                    contact_email = src.contact_email,
                    contact_phone = src.contact_phone,
                    address_line1 = src.address_line1,
                    address_line2 = src.address_line2,
                    city = src.city,
                    postcode = src.postcode,
                    country = src.country,
                    address = src.address,
                    license_start_date = src.license_start_date,
                    license_end_date = src.license_end_date,
                    notes = src.notes,
                    updated_at = src.updated_at
            WHEN NOT MATCHED THEN
                INSERT (
                    cms_customer_id, mobile_cms_id, store_id, customer_name, spos_customer_id,
                    trading_name, company_name, vat_registration_no, contact_no, email_address,
                    contact_name, contact_email, contact_phone, address_line1, address_line2,
                    city, postcode, country, address, license_start_date, license_end_date,
                    notes, created_at, updated_at
                )
                VALUES (
                    src.cms_customer_id, src.mobile_cms_id, src.store_id, src.customer_name, src.spos_customer_id,
                    src.trading_name, src.company_name, src.vat_registration_no, src.contact_no, src.email_address,
                    src.contact_name, src.contact_email, src.contact_phone, src.address_line1, src.address_line2,
                    src.city, src.postcode, src.country, src.address, src.license_start_date, src.license_end_date,
                    src.notes, src.created_at, src.updated_at
                );
            """,
            (
                row.get("cms_customer_id"),
                row.get("mobile_cms_id"),
                row.get("store_id"),
                row.get("customer_name"),
                row.get("spos_customer_id"),
                row.get("trading_name"),
                row.get("company_name"),
                row.get("vat_registration_no"),
                row.get("contact_no"),
                row.get("email_address"),
                row.get("contact_name"),
                row.get("contact_email"),
                row.get("contact_phone"),
                row.get("address_line1"),
                row.get("address_line2"),
                row.get("city"),
                row.get("postcode"),
                row.get("country"),
                row.get("address"),
                row.get("license_start_date"),
                row.get("license_end_date"),
                row.get("notes"),
                row.get("created_at"),
                row.get("updated_at"),
            ),
        )
        conn.commit()
        log_event(f"SQL Server customer profile upsert ok into dbo.{table}")
        return True, "ok"
    except Exception as e:
        log_event(f"SQL customer profile upsert failed: {e}")
        return False, f"SQL customer profile upsert failed: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass

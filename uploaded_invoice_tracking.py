"""Exact invoice references for UploadedFiles (queue and invoice DBs may differ)."""
from __future__ import annotations


def ensure_table(conn):
    conn.cursor().execute("""
        IF OBJECT_ID('dbo.UploadedFileInvoices', 'U') IS NULL
        BEGIN
            CREATE TABLE dbo.UploadedFileInvoices (
                UploadedFileId INT NOT NULL,
                InvoiceOrdinal INT NOT NULL,
                InvoiceDatabase NVARCHAR(128) NOT NULL,
                DocType NVARCHAR(64) NOT NULL,
                HeaderId INT NOT NULL,
                InvoiceFingerprint NVARCHAR(64) NULL,
                InvoiceNumber NVARCHAR(128) NULL,
                InvoiceDate NVARCHAR(64) NULL,
                LineIdStart INT NULL,
                LineIdEnd INT NULL,
                CONSTRAINT PK_UploadedFileInvoices PRIMARY KEY (UploadedFileId, InvoiceOrdinal)
            );
        END
    """)


def replace_links(conn, uploaded_file_id, references):
    """Replace this upload's links in the caller's status-update transaction."""
    cur = conn.cursor()
    cur.execute("DELETE FROM dbo.UploadedFileInvoices WHERE UploadedFileId=?", (uploaded_file_id,))
    for ordinal, ref in enumerate(references, 1):
        cur.execute("""
            INSERT INTO dbo.UploadedFileInvoices
                (UploadedFileId, InvoiceOrdinal, InvoiceDatabase, DocType, HeaderId,
                 InvoiceFingerprint, InvoiceNumber, InvoiceDate, LineIdStart, LineIdEnd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (uploaded_file_id, ordinal, ref['database'], ref['doc_type'], ref['header_id'],
              ref.get('fingerprint'), ref['invoice_number'], ref['invoice_date'],
              ref.get('line_id_start'), ref.get('line_id_end')))


def load_invoices(queue_conn, invoice_conn, uploaded_file_id, invoice_database, cms_customer_id):
    """Read every linked purchase invoice, scoped to the caller's customer.

    Never use the aggregate UploadedFiles line range or source filename to join.
    Connections are owned by the caller. Legacy uploads must be reprocessed to
    populate exact links; guessing membership from a range is unsafe.
    """
    cur = queue_conn.cursor()
    cur.execute("""
        SELECT HeaderId, InvoiceFingerprint FROM dbo.UploadedFileInvoices
        WHERE UploadedFileId=? AND InvoiceDatabase=? AND DocType='Purchase Invoice'
        ORDER BY InvoiceOrdinal
    """, (uploaded_file_id, invoice_database))
    refs = cur.fetchall()
    out = []
    seen = set()
    for header_id, fingerprint in refs:
        if header_id in seen:
            continue
        seen.add(header_id)
        cur = invoice_conn.cursor()
        cur.execute("""
            SELECT * FROM dbo.DocMate_invoice_headers
            WHERE id=? AND invoice_fingerprint=? AND cms_customer_id=?
        """, (header_id, fingerprint, cms_customer_id))
        row = cur.fetchone()
        if row is None:
            continue
        header = dict(zip([c[0] for c in cur.description], row))
        cur.execute("""
            SELECT * FROM dbo.DocMate_invoice_line_Data
            WHERE invoice_fingerprint=? AND cms_customer_id=?
            ORDER BY line_no, id
        """, (fingerprint, cms_customer_id))
        columns = [c[0] for c in cur.description]
        out.append({'header': header, 'items': [dict(zip(columns, r)) for r in cur.fetchall()]})
    return out

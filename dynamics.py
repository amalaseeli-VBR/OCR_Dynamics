"""Dynamics database configuration and SQL Server connection helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict


def read_credentials(
    app_dir: Path,
    yaml_parser: Callable[[str], Dict[str, str]],
    filename: str = "Dynamics_db_cred.yaml",
) -> Dict[str, str]:
    """Read Dynamics credentials using v1.18's intentionally small YAML parser."""
    try:
        path = Path(app_dir) / filename
        if not path.exists():
            return {}
        config = yaml_parser(path.read_text(encoding="utf-8", errors="ignore"))
        if config and not str(config.get("server") or "").strip():
            config["server"] = "."
        return config
    except Exception:
        return {}


def build_connection_string(config: Dict[str, str], database_override: str = "") -> str:
    server = (config.get("server") or ".").strip()
    port = (config.get("port") or "").strip()
    if port and "," not in server and ":" not in server and "\\" not in server:
        server = f"{server},{port}"
    database = (database_override or config.get("database") or "DocMate_Dynamics").strip()
    username = (config.get("username") or "").strip()
    password = (config.get("password") or "").strip()
    driver = (config.get("driver") or "ODBC Driver 17 for SQL Server").strip()
    encrypt = (config.get("encrypt") or "no").strip()
    trust_cert = (config.get("trust_server_certificate") or "yes").strip()
    authentication = (
        f"UID={username};PWD={password};" if username else "Trusted_Connection=yes;"
    )
    return (
        f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
        f"{authentication}Encrypt={encrypt};TrustServerCertificate={trust_cert};"
    )


def connect(config: Dict[str, str], database_override: str = ""):
    """Connect lazily so importing or testing the module does not require pyodbc."""
    import pyodbc

    try:
        timeout = int(float(config.get("timeout") or 3))
    except (TypeError, ValueError):
        timeout = 3
    return pyodbc.connect(
        build_connection_string(config, database_override=database_override),
        timeout=timeout,
        autocommit=True,
    )


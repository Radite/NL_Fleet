"""
Database access layer.

Two backends, selected via the DB_BACKEND environment variable:

  - "sqlite" (default) — the bundled demo database. Read-only enforcement
    is REAL here too: opened with SQLite's `mode=ro` URI flag, which makes
    the OS-level file handle read-only. Used for local trials, CI, and the
    test suite — no real credentials needed.

  - "sqlserver" — a real read-only login against your production
    reconciliation database, via pyodbc. This is what you point at real
    Pelican Energy data. The read-only boundary here is NOT an OS flag —
    it's the SQL Server login's grants (SELECT only, nothing else). That
    means the boundary is only as real as the GRANT script you actually ran
    against production. Prove it the same way the sqlite version proves
    it — see "Verifying the read-only boundary" in the README — don't take
    it on faith.

    Requires: `pip install pyodbc` (not in the base requirements.txt,
    since it needs the system ODBC Driver for SQL Server installed —
    see requirements-prod.txt and the Dockerfile).
"""

import os
from pathlib import Path

DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite").lower()
QUERY_TIMEOUT_SECONDS = int(os.environ.get("DB_QUERY_TIMEOUT_SECONDS", "10"))

# --- SQLite backend (demo / local dev / CI) ---------------------------------
SQLITE_DB_PATH = Path(__file__).parent.parent / "data" / "fleet_demo.db"


def _get_sqlite_connection():
    import sqlite3

    if not SQLITE_DB_PATH.exists():
        raise FileNotFoundError(
            f"{SQLITE_DB_PATH} does not exist. Run `python data/seed_db.py` first."
        )
    uri = f"file:{SQLITE_DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=QUERY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


# --- SQL Server backend (production) ----------------------------------------
def _get_sqlserver_connection():
    connection_string = os.environ.get("DB_CONNECTION_STRING")
    if not connection_string:
        raise RuntimeError(
            "DB_BACKEND=sqlserver but DB_CONNECTION_STRING is not set. In Azure App "
            "Service this should be a Key Vault reference app setting pointing at "
            "the fleet_nl_reader login — see README > Deploying to production."
        )

    import pyodbc  # imported lazily so the sqlite/demo path has no hard dependency

    # timeout= is the connection/login timeout; conn.timeout below caps how
    # long any single query is allowed to run once connected, so a
    # pathological query can't hang a worker indefinitely.
    conn = pyodbc.connect(connection_string, timeout=5)
    conn.timeout = QUERY_TIMEOUT_SECONDS
    return conn


def get_readonly_connection():
    if DB_BACKEND == "sqlserver":
        return _get_sqlserver_connection()
    if DB_BACKEND != "sqlite":
        raise ValueError(f"Unknown DB_BACKEND: {DB_BACKEND!r} (expected 'sqlite' or 'sqlserver')")
    return _get_sqlite_connection()


def execute_readonly_query(sql: str):
    """Runs a validated, read-only query and returns (columns, rows).

    On the sqlite backend, a write that somehow got past validate_query()
    raises sqlite3.OperationalError (OS-level read-only rejection). On the
    sqlserver backend, it raises pyodbc.Error with a permission-denied
    message from SQL Server (grant-level rejection). Either way this is the
    real backstop, not a formality — see this module's docstring.
    """
    conn = get_readonly_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        rows = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        return columns, [tuple(row) for row in rows]
    finally:
        conn.close()

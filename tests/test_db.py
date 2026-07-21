"""
Tests for the sqlite/sqlserver backend switch in app/db.py. These don't
require pyodbc or a real SQL Server to be present — they only check that
misconfiguration fails with a clear, actionable error rather than a
confusing stack trace, and that the default (sqlite) path still works.
"""

import os
import pytest

import app.db as db_module


def test_default_backend_is_sqlite():
    assert db_module.DB_BACKEND == "sqlite"


def test_sqlite_backend_opens_the_seeded_demo_db():
    conn = db_module.get_readonly_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM devices")
        (count,) = cursor.fetchone()
        assert count > 0
    finally:
        conn.close()


def test_sqlserver_backend_without_connection_string_fails_clearly(monkeypatch):
    monkeypatch.setattr(db_module, "DB_BACKEND", "sqlserver")
    monkeypatch.delenv("DB_CONNECTION_STRING", raising=False)
    with pytest.raises(RuntimeError, match="DB_CONNECTION_STRING is not set"):
        db_module.get_readonly_connection()


def test_unknown_backend_rejected(monkeypatch):
    monkeypatch.setattr(db_module, "DB_BACKEND", "oracle")
    with pytest.raises(ValueError, match="Unknown DB_BACKEND"):
        db_module.get_readonly_connection()

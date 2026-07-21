"""
Validation layer for model-generated SQL.

IMPORTANT — read this before trusting this module:
This is a defense-in-depth check, not the safety boundary. The actual
boundary is the read-only DB credential in app/db.py. A sufficiently
adversarial prompt injection (e.g. text smuggled in via a hostname or
department field that the model later echoes into a query) could in
principle produce SQL that slips past a regex/token check. It CANNOT slip
past a database connection that only has SELECT grants. Always run both
layers — never one instead of the other.

--- 2026-07 update: string-literal sanitization ---
The regex-based checks below (LIMIT detection, table extraction, forbidden
keyword scan) originally ran against the raw SQL text. That meant a query
like:

    SELECT * FROM devices WHERE notes = 'see LIMIT 5 in ticket #4'

would satisfy the `has_limit` regex even though there is no real LIMIT
clause — the digits inside the string literal were enough to match. That's
a fail-OPEN bug (an unbounded query gets waved through), which is the
dangerous direction for a defense-in-depth layer to fail in.

`_sanitize_string_literals()` walks the sqlparse token stream and blanks
out the *contents* of string literals (keeping the surrounding quotes, so
statement shape doesn't change) before any of those regex checks run. Real
SQL syntax typed by the model is untouched. Comments are deliberately left
alone — a comment is still treated as an unsafe place to hide a forbidden
keyword, and there's a regression test guarding that behavior
specifically (`test_comment_smuggled_ddl_still_caught_by_keyword_scan`).

--- 2026-07 attempted update: LIMIT -> TOP (reverted) ---
This was briefly changed to check for T-SQL's "SELECT TOP N ..." in
anticipation of DB_BACKEND=sqlserver. Reverted: prompts.py, the few-shot
examples, and claude_client.py's mock mode were never updated to match,
and DB_BACKEND still defaults to sqlite locally, so the TOP check broke
local dev outright (SQLite doesn't support TOP). Do the LIMIT->TOP switch
as one atomic change across validator.py + prompts.py + claude_client.py
+ tests, together with actually setting DB_BACKEND=sqlserver — not here
in isolation.
"""

import re
from dataclasses import dataclass
import sqlparse
from sqlparse.sql import Statement
from sqlparse.tokens import DDL, DML, Token

from app.schema import ALLOWED_TABLES

MAX_ROWS = 500

FORBIDDEN_STATEMENT_TYPES = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "REPLACE", "MERGE", "GRANT", "REVOKE", "ATTACH", "DETACH",
    "PRAGMA", "VACUUM",
}


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def _statement_count(sql: str) -> int:
    # sqlparse.split respects string literals and comments, unlike a naive
    # split on ';' — this is what actually catches stacked-query injection.
    statements = [s for s in sqlparse.split(sql) if s.strip()]
    return len(statements)


def _get_statement_keyword(parsed: Statement) -> str:
    for token in parsed.tokens:
        if token.ttype in (DML, DDL):
            return token.value.upper()
    return ""


def _sanitize_string_literals(sql: str) -> str:
    """Blanks the contents of string literals so later regex checks can't be
    fooled by user-supplied text that happens to contain SQL-looking
    substrings (e.g. a LIMIT-looking number, or a fake table name) inside a
    quoted value. Comments are intentionally left untouched."""
    parsed = sqlparse.parse(sql)
    if not parsed:
        return sql
    out = []
    for token in parsed[0].flatten():
        if token.ttype in Token.Literal.String:
            value = token.value
            if len(value) >= 2:
                out.append(value[0] + "_" * (len(value) - 2) + value[-1])
            else:
                out.append(value)
        else:
            out.append(token.value)
    return "".join(out)


def _extract_referenced_tables(sql: str) -> set:
    # Deliberately simple regex extraction. Good enough for a small, known
    # schema of 4-5 tables. If your schema grows past ~10 tables or gets
    # complex joins/subqueries, swap this for a real SQL parser walk
    # (sqlparse gives you the token tree — walk it instead of regexing).
    pattern = re.compile(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)
    return {m.group(1) for m in pattern.finditer(sql)}


def validate_query(sql: str) -> ValidationResult:
    if sql is None or not str(sql).strip():
        return ValidationResult(False, "No query was produced.")

    sql = sql.strip()

    if _statement_count(sql) != 1:
        return ValidationResult(False, "Exactly one SQL statement is permitted (no stacked queries).")

    parsed = sqlparse.parse(sql)[0]
    keyword = _get_statement_keyword(parsed)

    if keyword != "SELECT":
        return ValidationResult(
            False,
            f"Only SELECT statements are permitted (got: {keyword or 'unrecognized statement'})."
        )

    # Belt-and-suspenders keyword scan even after confirming it's a SELECT —
    # catches things like a SELECT with a smuggled DDL in a subquery comment.
    # Runs against the raw (not literal-sanitized) SQL on purpose: a comment
    # is not a safe place to hide a forbidden keyword either.
    upper_sql = sql.upper()
    for forbidden in FORBIDDEN_STATEMENT_TYPES:
        if re.search(rf"\b{forbidden}\b", upper_sql):
            return ValidationResult(False, f"Query contains a disallowed keyword: {forbidden}.")

    # From here on, work against a version of the SQL with string-literal
    # contents blanked out — see module docstring for why.
    sanitized_sql = _sanitize_string_literals(sql)
    sanitized_upper = sanitized_sql.upper()

    referenced_tables = _extract_referenced_tables(sanitized_sql)
    unknown_tables = referenced_tables - ALLOWED_TABLES
    if unknown_tables:
        return ValidationResult(False, f"References unknown table(s): {sorted(unknown_tables)}.")

    if not referenced_tables:
        return ValidationResult(False, "Could not identify a FROM/JOIN target — rejected as unsafe to run.")

    has_limit = re.search(r"\bLIMIT\s+\d+\b", sanitized_upper)
    is_single_row_aggregate = re.search(
        r"\b(COUNT|AVG|SUM|MIN|MAX)\s*\(", sanitized_upper
    ) and not re.search(r"\bGROUP\s+BY\b", sanitized_upper)

    if not has_limit and not is_single_row_aggregate:
        return ValidationResult(
            False,
            "Query has no LIMIT clause and isn't a single-row aggregate — rejected as a safety measure."
        )

    if has_limit:
        limit_value = int(has_limit.group(0).split()[-1])
        if limit_value > MAX_ROWS:
            return ValidationResult(False, f"LIMIT {limit_value} exceeds the max of {MAX_ROWS} rows.")

    return ValidationResult(True)
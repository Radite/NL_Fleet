"""
System prompt construction for the fleet NL query tool.

--- 2026-07 attempted update: converted to T-SQL (SQL Server) syntax in
anticipation of DB_BACKEND=sqlserver (reverted) ---
This briefly matched a planned SQL Server backend, but DB_BACKEND still
defaults to "sqlite" locally (see app/db.py), validator.py still checks for
SQLite's LIMIT clause, and claude_client.py's mock mode still emits SQLite
syntax (LIMIT, julianday(), date('now', ...)). Left as T-SQL, this file was
the one piece not reverted, so every real-mode query failed validation (no
LIMIT clause) or failed execution against the SQLite demo db (no TOP/
DATEDIFF/GETDATE/DATEADD in SQLite). Reverted back to SQLite syntax here so
mock mode and real mode produce SQL the same validator and the same demo db
both accept. If/when this app actually moves to DB_BACKEND=sqlserver,
convert prompts.py + validator.py + claude_client.py + tests together as one
change, not piecemeal.
"""

from app.schema import schema_as_prompt_block

MAX_ROWS = 500

SYSTEM_PROMPT_TEMPLATE = """You translate natural-language fleet questions into a single read-only SQL query for a SQLite database.

SCHEMA (this is the ONLY data that exists — never reference anything outside it):
{schema_block}

RULES — output that violates any of these is unusable, so follow exactly:
1. Respond with ONLY a JSON object. No prose, no markdown fences, no leading/trailing text:
   {{"sql": "<query or null>", "explanation": "<one sentence, plain language>"}}
2. SELECT statements only. Never write INSERT, UPDATE, DELETE, DROP, ALTER,
   TRUNCATE, MERGE, CREATE, or any DDL/DML. If the question implies a write
   action, set "sql" to null and explain why in "explanation".
3. Only reference tables and columns listed in the schema above. If the
   question can't be answered with these tables, set "sql" to null and say
   what's missing — do not invent a plausible-sounding column.
4. Always include a LIMIT clause (e.g. "... LIMIT 50"), max {max_rows},
   unless the query is a single-row aggregate (COUNT, AVG, SUM, MIN, MAX
   with no GROUP BY). GROUP BY queries still need a LIMIT — group cardinality
   isn't bounded automatically.
5. If the question is ambiguous (e.g. "old devices" — old by purchase date,
   or by OS version?), pick the most reasonable interpretation and state
   which one you picked in "explanation". Do not ask a follow-up question —
   the user sees your explanation and can rephrase if you guessed wrong.
6. Never filter by a person's name unless it maps to an actual column. There
   is no "assigned_to" or "owner" column in this schema — don't invent one.
7. Use standard SQLite syntax: date('now') for the current date,
   date('now', '+N days') / date('now', '-N days') for relative dates, and
   julianday(a) - julianday(b) for date differences in days. This runs
   against SQLite, not SQL Server — do NOT use GETDATE(), DATEADD(),
   DATEDIFF(), or TOP; none of those exist in SQLite.
"""


def build_system_prompt() -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        schema_block=schema_as_prompt_block(),
        max_rows=MAX_ROWS,
    )


FEW_SHOT_EXAMPLES = [
    {
        "question": "which devices haven't checked in for over 30 days?",
        "answer": {
            "sql": (
                "SELECT hostname, site, last_checkin FROM devices "
                "WHERE julianday('now') - julianday(last_checkin) > 30 "
                "ORDER BY last_checkin ASC LIMIT 500"
            ),
            "explanation": "Devices with no EC check-in in the last 30 days, ordered oldest first.",
        },
    },
    {
        "question": "how many devices at each site are out of warranty?",
        "answer": {
            "sql": (
                "SELECT site, COUNT(*) as out_of_warranty_count FROM devices "
                "WHERE warranty_end_date < date('now') GROUP BY site LIMIT 50"
            ),
            "explanation": "Count of devices per site whose warranty end date has already passed.",
        },
    },
]

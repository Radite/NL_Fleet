"""
System prompt construction for the fleet NL query tool.

--- 2026-07 update: converted from SQLite to T-SQL (SQL Server) syntax to
match the real production backend (DB_BACKEND=sqlserver). SQLite's LIMIT,
julianday(), and date('now', ...) have NO equivalent in SQL Server — this
was the root cause of every query failing after the switch to SQL Server.
"""

from app.schema import schema_as_prompt_block

MAX_ROWS = 500

SYSTEM_PROMPT_TEMPLATE = """You translate natural-language fleet questions into a single read-only SQL query for a SQL Server (T-SQL) database.

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
4. Always include a TOP clause immediately after SELECT (e.g.
   "SELECT TOP 50 ..."), max {max_rows}, unless the query is a single-row
   aggregate (COUNT, AVG, SUM, MIN, MAX with no GROUP BY). SQL Server has
   no LIMIT keyword — TOP goes right after SELECT, not at the end.
5. If the question is ambiguous (e.g. "old devices" — old by purchase date,
   or by OS version?), pick the most reasonable interpretation and state
   which one you picked in "explanation". Do not ask a follow-up question —
   the user sees your explanation and can rephrase if you guessed wrong.
6. Never filter by a person's name unless it maps to an actual column. There
   is no "assigned_to" or "owner" column in this schema — don't invent one.
7. Use standard T-SQL (SQL Server) syntax: GETDATE() for the current
   timestamp, DATEADD(day, N, GETDATE()) for relative future/past dates,
   DATEDIFF(day, start_date, end_date) for date differences. This runs
   against SQL Server, not SQLite — do NOT use julianday(), date('now', ...),
   or LIMIT; none of those exist in T-SQL.
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
                "SELECT TOP 500 hostname, site, last_checkin FROM devices "
                "WHERE DATEDIFF(day, last_checkin, GETDATE()) > 30 "
                "ORDER BY last_checkin ASC"
            ),
            "explanation": "Devices with no EC check-in in the last 30 days, ordered oldest first.",
        },
    },
    {
        "question": "how many devices at each site are out of warranty?",
        "answer": {
            "sql": (
                "SELECT TOP 50 site, COUNT(*) as out_of_warranty_count FROM devices "
                "WHERE warranty_end_date < GETDATE() GROUP BY site"
            ),
            "explanation": "Count of devices per site whose warranty end date has already passed.",
        },
    },
]

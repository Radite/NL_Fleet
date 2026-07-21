"""
FastAPI application exposing the fleet NL query tool.

Run: uvicorn app.main:app --reload --port 8000
Then open http://localhost:8000 for the UI, or POST directly to /ask.

--- 2026-07 hardening pass ---
Four gaps identified in review, closed here:
  1. No auth at all on /ask -> optional shared-secret API key (APP_API_KEY).
  2. No input length cap -> could balloon prompt/API cost -> capped question length.
  3. No rate limiting -> simple in-memory per-IP sliding window.
  4. Raw exception text (incl. file paths) returned to the client on DB
     errors -> now logged in full server-side, generic message to the client.

None of this is a substitute for real org SSO if you deploy this beyond a
handful of trusted users on a private network — see README for that path.
"""

import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # picks up a .env file in the working directory, if present

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.claude_client import call_claude
from app.validator import validate_query
from app.db import execute_readonly_query

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fleet_nl_query")

app = FastAPI(title="Fleet NL Query Tool")

STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# --- Auth ---------------------------------------------------------------
# Production path: Azure App Service Authentication ("Easy Auth") with
# Entra ID, configured at the platform level (see README > Deploying to
# production). App Service rejects unauthenticated requests before they
# ever reach this process, and injects the signed-in user's identity as
# the X-MS-CLIENT-PRINCIPAL-NAME header — read below for audit logging.
# There is nothing to enable here in code; this app only *reads* that
# header if present.
#
# Local-dev / non-Azure fallback: set APP_API_KEY to require a shared
# "X-API-Key" header instead. Left unset entirely, the app still runs
# (useful for local dev and the offline test suite) but logs a warning on
# every request so an open deployment doesn't go unnoticed.
APP_API_KEY = os.environ.get("APP_API_KEY")
EASY_AUTH_HEADER = "X-MS-CLIENT-PRINCIPAL-NAME"

# --- Rate limiting (in-memory, per-process) ---------------------------------
# Good enough for a single-instance internal tool. If this ever runs behind
# a load balancer with multiple workers, move this to Redis or push it to
# the reverse proxy instead — an in-process dict won't be shared across
# workers.
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("APP_RATE_LIMIT", "20"))
RATE_LIMIT_WINDOW_SECONDS = 60
_request_log: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(client_id: str) -> bool:
    now = time.monotonic()
    window = _request_log[client_id]
    while window and now - window[0] > RATE_LIMIT_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    window.append(now)
    return True


MAX_QUESTION_LENGTH = 500


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_LENGTH)


class AskResponse(BaseModel):
    status: str
    sql: str | None = None
    explanation: str = ""
    reason: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    row_count: int = 0


@app.get("/")
def serve_ui():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/ask", response_model=AskResponse)
def ask(
    request: AskRequest,
    http_request: Request,
    x_api_key: str | None = Header(default=None),
    x_ms_client_principal_name: str | None = Header(default=None),
):
    # If App Service Easy Auth is in front of this app, it already rejected
    # unauthenticated requests before we got here, and it hands us the
    # signed-in user's identity — use that for the audit trail and skip the
    # (now redundant) shared-key check.
    if x_ms_client_principal_name:
        client_id = x_ms_client_principal_name
    else:
        client_id = http_request.client.host if http_request.client else "unknown"

        if APP_API_KEY:
            if x_api_key != APP_API_KEY:
                logger.warning("Rejected request from %s: missing/invalid API key", client_id)
                raise HTTPException(status_code=401, detail="Missing or invalid API key.")
        else:
            logger.warning(
                "Neither Easy Auth nor APP_API_KEY is active — /ask is running with no "
                "authentication. Fine for local dev; not fine once this is reachable by "
                "anyone else. See README > Deploying to production."
            )

    if not _check_rate_limit(client_id):
        logger.warning("Rate limit exceeded for %s", client_id)
        raise HTTPException(status_code=429, detail="Too many requests — please slow down.")

    # Every request is logged with the client, the question, and the eventual
    # SQL/outcome — this audit trail matters as much as the safety checks
    # themselves. If the tool ever gives a wrong answer, you need to be able
    # to reconstruct exactly what query it ran, for whom, and why.
    logger.info("Question from %s: %s", client_id, request.question)

    raw_response = call_claude(request.question)

    try:
        parsed = json.loads(raw_response)
    except json.JSONDecodeError:
        logger.warning("Non-JSON response from model: %s", raw_response)
        return AskResponse(
            status="error",
            explanation="The model did not return a parseable response. Try rephrasing the question.",
        )

    sql = parsed.get("sql")
    explanation = parsed.get("explanation", "")

    if sql is None:
        logger.info("Model declined to produce SQL: %s", explanation)
        return AskResponse(status="declined", explanation=explanation)

    validation = validate_query(sql)
    if not validation.ok:
        logger.warning("Rejected query: %s | reason: %s", sql, validation.reason)
        return AskResponse(
            status="rejected",
            sql=sql,
            explanation=explanation,
            reason=validation.reason,
        )

    try:
        columns, rows = execute_readonly_query(sql)
    except Exception as exc:
        # Log the real error server-side (may contain file paths, driver
        # detail, etc.) but never hand that raw text back to the client.
        logger.error("Execution failed for query: %s | error: %s", sql, exc)
        return AskResponse(
            status="error",
            sql=sql,
            explanation=explanation,
            reason="The query failed to execute. This has been logged for review.",
        )

    logger.info("Query succeeded for %s: %s rows returned", client_id, len(rows))
    return AskResponse(
        status="ok",
        sql=sql,
        explanation=explanation,
        columns=columns,
        rows=[list(row) for row in rows],
        row_count=len(rows),
    )

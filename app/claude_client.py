"""
Thin wrapper around the Anthropic API for NL -> SQL translation.

Two modes:
  - Real mode: ANTHROPIC_API_KEY is set -> calls the actual API, handles
    any phrasing.
  - Mock mode: no key set -> pattern-matches a curated list of common
    fleet questions below. This is NOT a substitute for real translation —
    it only recognizes the phrasings listed in MOCK_COMMAND_REFERENCE
    (or close variants using the same keywords). Anything else returns a
    "declined" response naming what mock mode doesn't cover, so the
    failure is visible rather than silently wrong.

--- 2026-07 update: expanded from 5 to ~20 canned patterns. ---

--- 2026-07 attempted update: LIMIT/julianday -> TOP/DATEDIFF/GETDATE/
DATEADD (reverted) ---
This file was briefly converted to T-SQL in anticipation of
DB_BACKEND=sqlserver, at the same time as a matching (and since-reverted)
change to app/validator.py. Reverted here too: DB_BACKEND still defaults
to sqlite locally, prompts.py's system prompt and few-shot examples still
tell the real API to use SQLite syntax, and validator.py checks for LIMIT
again — so mock mode needs to match, or every canned response fails at
execution against the local SQLite demo db (SQLite has no TOP/DATEDIFF/
GETDATE/DATEADD). If/when this app actually moves to DB_BACKEND=sqlserver,
convert prompts.py + validator.py + this file + the tests together as one
change, not piecemeal.
"""

import os
import json
import re

from app.prompts import build_system_prompt

MODEL = "claude-sonnet-4-6"

# Real site names as populated by the sync job's hostname-convention
# parser — keep this in sync with sync/sync_devices.py's DIRECT_SITE_CODES
# / FTCI_SITE_CODES if that list ever changes.
KNOWN_SITES = [
    "Providenciales", "Grand Turk", "South Caicos", "Salt Cay", "North Caicos",
]

# Human-readable list of what mock mode actually recognizes — surfaced in
# the UI/README so people aren't guessing at exact phrasing. Keep this in
# sync with the patterns in _mock_response() below.
MOCK_COMMAND_REFERENCE = [
    "which devices are expiring soon on warranty?",
    "which devices are out of warranty?",
    "which devices haven't checked in for 30 days?",
    "which devices are offline?",
    "which devices are unmanaged?",
    "how many devices per site?",
    "how many devices per department?",
    "how many devices per type?",
    "how many devices are healthy?",
    "how many devices total?",
    "which devices have missing critical patches?",
    "which devices are not patch compliant?",
    "which printers are low on toner?",
    "which printers are offline?",
    "which devices have no site assigned?",
    "which devices need a hostname rename?",
    "what operating systems are in use?",
    "how many devices are in Providenciales?",  # or any KNOWN_SITES name
    "delete / remove / drop anything",  # always declined
]


def _mock_response(user_message: str) -> str:
    q = user_message.lower()

    # --- Writes: always declined, checked first so nothing below can
    # accidentally produce a destructive-sounding query ---
    if any(w in q for w in ("delete", "remove", "drop", "update ", "insert ")):
        return json.dumps({
            "sql": None,
            "explanation": "This tool is read-only and cannot make changes to fleet data.",
        })

    # --- Patch compliance (checked before generic "how many") ---
    if "missing" in q and ("critical" in q or "patch" in q):
        return json.dumps({
            "sql": "SELECT d.hostname, d.site, p.missing_critical_patches, p.scan_date "
                   "FROM devices d JOIN patch_compliance p ON d.device_id = p.device_id "
                   "WHERE p.missing_critical_patches > 0 "
                   "ORDER BY p.missing_critical_patches DESC LIMIT 500",
            "explanation": "Devices with one or more missing critical patches, most-missing first.",
        })

    if "compliant" in q or ("patch" in q and ("not" in q or "non" in q)):
        return json.dumps({
            "sql": "SELECT d.hostname, d.site, p.missing_total_patches, p.scan_date "
                   "FROM devices d JOIN patch_compliance p ON d.device_id = p.device_id "
                   "WHERE p.compliant = 0 ORDER BY p.missing_total_patches DESC LIMIT 500",
            "explanation": "Devices flagged as not patch-compliant, most missing patches first.",
        })

    # --- Warranty ---
    if "warranty" in q and ("expir" in q or "soon" in q):
        return json.dumps({
            "sql": "SELECT hostname, site, warranty_end_date FROM devices "
                   "WHERE warranty_end_date < date('now', '+90 days') "
                   "ORDER BY warranty_end_date ASC LIMIT 500",
            "explanation": "Devices with warranty ending within 90 days, sorted soonest first.",
        })

    if "warranty" in q and ("out" in q or "expired" in q):
        return json.dumps({
            "sql": "SELECT hostname, site, warranty_end_date FROM devices "
                   "WHERE warranty_end_date < date('now') ORDER BY warranty_end_date ASC LIMIT 500",
            "explanation": "Devices whose warranty has already expired, sorted longest-expired first.",
        })

    # --- Agent status ---
    if "check" in q and ("30 day" in q or "haven't" in q or "stale" in q):
        return json.dumps({
            "sql": "SELECT hostname, site, last_checkin, agent_status FROM devices "
                   "WHERE julianday('now') - julianday(last_checkin) > 30 "
                   "ORDER BY last_checkin ASC LIMIT 500",
            "explanation": "Devices with no EC check-in in over 30 days.",
        })

    if "offline" in q and "printer" not in q:
        return json.dumps({
            "sql": "SELECT hostname, site, last_checkin FROM devices "
                   "WHERE agent_status = 'offline' ORDER BY last_checkin ASC LIMIT 500",
            "explanation": "Devices currently flagged offline.",
        })

    if "unmanaged" in q:
        return json.dumps({
            "sql": "SELECT hostname, site, device_type FROM devices "
                   "WHERE agent_status = 'unmanaged' LIMIT 500",
            "explanation": "Devices flagged as unmanaged (no active EC agent).",
        })

    if "healthy" in q:
        return json.dumps({
            "sql": "SELECT COUNT(*) as healthy_count FROM devices WHERE agent_status = 'healthy'",
            "explanation": "Count of devices currently reporting healthy agent status.",
        })

    # --- Site-specific: any known site name mentioned ---
    for site in KNOWN_SITES:
        if site.lower() in q:
            return json.dumps({
                "sql": f"SELECT hostname, device_type, agent_status FROM devices "
                       f"WHERE site = '{site}' LIMIT 500",
                "explanation": f"Devices located at {site}.",
            })

    # --- Data-quality follow-ups from the sync audit ---
    if "no site" in q or ("site" in q and "missing" in q) or ("site" in q and "null" in q):
        return json.dumps({
            "sql": "SELECT hostname, device_type FROM devices WHERE site IS NULL LIMIT 500",
            "explanation": "Devices with no site inferred from hostname — naming convention gap.",
        })

    if "rename" in q or ("desktop-" in q) or ("default" in q and "hostname" in q):
        return json.dumps({
            "sql": "SELECT hostname, device_id FROM devices "
                   "WHERE hostname LIKE 'DESKTOP-%' OR hostname LIKE 'PC-%' LIMIT 500",
            "explanation": "Devices with un-renamed default Windows hostnames, flagged for manual rename.",
        })

    # --- Printers ---
    if "printer" in q and ("toner" in q or "low" in q):
        return json.dumps({
            "sql": "SELECT printer_id, site, department, toner_level_percent FROM printers "
                   "WHERE toner_level_percent < 20 ORDER BY toner_level_percent ASC LIMIT 500",
            "explanation": "Printers with toner below 20%, interpreted 'low' as under 20%.",
        })

    if "printer" in q and "offline" in q:
        return json.dumps({
            "sql": "SELECT printer_id, site, department, last_seen FROM printers "
                   "WHERE status = 'offline' ORDER BY last_seen ASC LIMIT 500",
            "explanation": "Printers currently reporting offline.",
        })

    # --- Grouping / counts (checked after more specific patterns above) ---
    if "how many" in q and "department" in q:
        return json.dumps({
            "sql": "SELECT department, COUNT(*) as device_count FROM devices "
                   "GROUP BY department LIMIT 50",
            "explanation": "Device count grouped by department.",
        })

    if "how many" in q and ("type" in q or "laptop" in q or "desktop" in q
                            or "tablet" in q or "server" in q):
        return json.dumps({
            "sql": "SELECT device_type, COUNT(*) as device_count FROM devices "
                   "GROUP BY device_type LIMIT 50",
            "explanation": "Device count grouped by device type.",
        })

    if "how many" in q and "site" in q:
        return json.dumps({
            "sql": "SELECT site, COUNT(*) as device_count FROM devices GROUP BY site LIMIT 50",
            "explanation": "Device count grouped by site.",
        })

    if "how many" in q and ("total" in q or "devices" in q):
        return json.dumps({
            "sql": "SELECT COUNT(*) as total_devices FROM devices",
            "explanation": "Total device count across the fleet.",
        })

    if "operating system" in q or "os version" in q or ("what os" in q):
        return json.dumps({
            "sql": "SELECT os_version, COUNT(*) as device_count FROM devices "
                   "GROUP BY os_version ORDER BY device_count DESC LIMIT 50",
            "explanation": "Device count grouped by operating system version.",
        })

    return json.dumps({
        "sql": None,
        "explanation": "Mock mode doesn't recognize this phrasing. See MOCK_COMMAND_REFERENCE in "
                        "claude_client.py for the full list of recognized questions, or set "
                        "ANTHROPIC_API_KEY for real translation of any phrasing.",
    })


def call_claude(user_message: str) -> str:
    """Returns the raw text response from Claude (expected to be a JSON
    string per the system prompt's contract)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")

    if not api_key:
        return _mock_response(user_message)

    import anthropic  # imported lazily so mock mode has no hard dependency
    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": user_message}],
    )

    text_blocks = [block.text for block in response.content if block.type == "text"]
    raw = "".join(text_blocks).strip()

    # Models occasionally wrap JSON in markdown fences despite instructions —
    # strip defensively rather than failing the whole request over it.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    return raw
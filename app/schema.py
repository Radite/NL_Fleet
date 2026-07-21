"""
Schema definition for the fleet NL query tool.

This is intentionally a curated subset of what actually exists in EC /
ServiceDesk Plus / TechDirect — not a mirror of the full database. Only
columns you want to be askable-about should appear here. Anything you leave
out is invisible to the model; that's a feature, not a gap to fill in later.

To point this at your real Pelican Energy data:
  - Replace table/column names to match your reconciliation views.
  - If a column is derived (e.g. "days until warranty expires"), either
    materialize it as a real column/view in the DB, or add it here with a
    note that it must be computed via a SQL expression (e.g. DATEDIFF).
  - Keep descriptions short and factual — they go directly into the prompt
    and get charged against your context budget on every call.
"""

DB_SCHEMA = {
    "devices": {
        "description": "One row per endpoint in the reconciled EC fleet.",
        "columns": {
            "device_id": "text, primary key",
            "hostname": "text",
            "asset_tag": "text — matches Dell service tag where applicable",
            "site": "text — e.g. 'Provo', 'Grand Turk', 'Head Office'",
            "department": "text",
            "device_type": "text — one of: laptop, desktop, server, tablet, printer",
            "os_version": "text",
            "last_checkin": "datetime — last EC agent check-in timestamp",
            "agent_status": "text — one of: healthy, stale, offline, unmanaged",
            "purchase_date": "date, nullable",
            "warranty_end_date": "date, nullable",
        },
    },
    "patch_compliance": {
        "description": "Latest patch scan result per device, one row per device per scan.",
        "columns": {
            "device_id": "text, foreign key -> devices.device_id",
            "scan_date": "datetime",
            "missing_critical_patches": "integer",
            "missing_total_patches": "integer",
            "compliant": "boolean — true if missing_critical_patches = 0",
        },
    },
    "warranty_lookup": {
        "description": "Cached Dell TechDirect warranty pulls, refreshed weekly via the warranty comparison job.",
        "columns": {
            "asset_tag": "text",
            "warranty_status": "text — Active, Expired, Expiring Soon",
            "days_remaining": "integer, nullable",
            "last_refreshed": "datetime",
        },
    },
    "printers": {
        "description": "Printer fleet inventory from the SNMP monitoring scripts and department mapping.",
        "columns": {
            "printer_id": "text, primary key",
            "site": "text",
            "department": "text — assigned via DeptPrinterMap",
            "model": "text",
            "toner_level_percent": "integer, nullable",
            "last_seen": "datetime",
            "status": "text — one of: online, offline, error",
        },
    },
}

ALLOWED_TABLES = set(DB_SCHEMA.keys())
ALLOWED_COLUMNS = {table: set(cols.keys()) for table, cols in DB_SCHEMA.items()}


def schema_as_prompt_block() -> str:
    """Renders the schema as compact text for the system prompt — cheaper in
    tokens than raw JSON and just as unambiguous for the model to parse."""
    lines = []
    for table, meta in DB_SCHEMA.items():
        lines.append(f"TABLE {table} — {meta['description']}")
        for col, desc in meta["columns"].items():
            lines.append(f"  - {col}: {desc}")
    return "\n".join(lines)

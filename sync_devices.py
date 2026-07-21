"""
Scheduled job: pulls computer inventory from Endpoint Central (via
ec_client.get_all_computers) and upserts it into the `devices` table.

--- 2026-07 update: field mappings below are now based on a REAL response
from the customer's EC instance, not guessed field names. What's still
genuinely unresolved is called out explicitly rather than guessed — see
"KNOWN GAPS" below.

KNOWN GAPS — do not silently paper over these:
  1. asset_tag (Dell service tag): NOT present anywhere in the SoM Computers
     response. Left as NULL here. If warranty_lookup needs to join to
     devices, it may need to join on hostname instead of asset_tag, or
     asset_tag needs sourcing from a different EC API / a separate Dell
     inventory pull. Confirm with whoever owns the TechDirect warranty job.
  2. department: NOT present in this response at all (customer_name is an
     EC tenant label, always "DC_CUSTOMER" in samples seen — NOT a
     department, deliberately not used here). Left as NULL. If department
     data exists anywhere, it's a separate source, not this sync job.
  3. site: inferred from hostname naming CONVENTION, not a real EC field —
     inherently fragile. A hostname with no recognized site prefix (direct
     or FTCI) gets site=NULL. Any unmapped site is logged, not guessed.
  4. device_type: catch-all — LT/LPT=laptop, DTC/DT=desktop, TAB=tablet,
     SVR=server, and anything else defaults to "server". Confirmed after
     auditing the "server" bucket against a real 446-device sync and
     finding it was overcounting real laptops/desktops/tablets. Two
     exceptions carved out of the catch-all after that audit:
       - Un-renamed default Windows hostnames (DESKTOP-*, PC-*) are
         EXCLUDED from classification (device_type/site both NULL) rather
         than guessed — confirmed policy is to flag these for a manual
         rename, not assume desktop/laptop.
       - Golden-image/template machines (hostname contains GOLD/TMPLTE/
         TEMPLATE/BASEOS) are skipped from the sync ENTIRELY — no devices
         row at all, since they're OS deployment templates, not real
         in-service endpoints.
     Still worth re-auditing device_type distribution periodically — the
     catch-all can still misclassify a genuinely new naming pattern as
     "server" until someone spots it, same as it did here.
"""

import logging
import os
import sys
from datetime import datetime, timezone

import pyodbc

from sync.ec_client import get_all_computers, get_session

logger = logging.getLogger("ec_sync")

EC_SYNC_DB_CONNECTION_STRING = os.environ.get("EC_SYNC_DB_CONNECTION_STRING")

MERGE_SQL = """
MERGE dbo.devices AS target
USING (SELECT ? AS device_id, ? AS hostname, ? AS asset_tag, ? AS site,
              ? AS department, ? AS device_type, ? AS os_version,
              ? AS last_checkin, ? AS agent_status) AS source
ON target.device_id = source.device_id
WHEN MATCHED THEN
    UPDATE SET hostname = source.hostname, asset_tag = source.asset_tag,
               site = source.site, department = source.department,
               device_type = source.device_type, os_version = source.os_version,
               last_checkin = source.last_checkin, agent_status = source.agent_status
WHEN NOT MATCHED THEN
    INSERT (device_id, hostname, asset_tag, site, department, device_type,
            os_version, last_checkin, agent_status)
    VALUES (source.device_id, source.hostname, source.asset_tag, source.site,
            source.department, source.device_type, source.os_version,
            source.last_checkin, source.agent_status);
"""

# --- Confirmed site/device-type naming convention -------------------------
DIRECT_SITE_CODES = {
    "PL": "Providenciales",
    "GT": "Grand Turk",
    "SC": "South Caicos",
    "SL": "Salt Cay",
    "NC": "North Caicos",
}

FTCI_SITE_CODES = {
    "PLS": "Providenciales",
    "NCS": "North Caicos",
    "XSC": "South Caicos",
    "SCS": "South Caicos",  # confirmed synonym for XSC — both mean South Caicos on FTCI hosts
    "SLC": "Salt Cay",
    "GDT": "Grand Turk",
}

# EC's own documented enums (from the instance's API doc, confirmed)
LIVE_STATUS_LIVE = 1
LIVE_STATUS_DOWN = 2
LIVE_STATUS_UNKNOWN = 3

INSTALL_STATUS_INSTALLED = 22
INSTALL_STATUS_YET_TO_INSTALL = 21
INSTALL_STATUS_UNINSTALLED = 23
INSTALL_STATUS_YET_TO_UNINSTALL = 24
INSTALL_STATUS_FAILURE = 29

# Thresholds below are OUR bucketing (healthy/stale/offline/unmanaged),
# not an EC field. These are a starting judgment call, not a confirmed
# business rule — adjust after looking at real distribution of
# agent_last_contact_time across your fleet.
STALE_AFTER_DAYS = 7
OFFLINE_AFTER_DAYS = 30


# Confirmed: exclude golden-image/template machines from the sync entirely
# — these are OS deployment templates, not real in-service endpoints, and
# shouldn't appear in fleet counts or query results at all.
EXCLUDED_HOSTNAME_KEYWORDS = ["GOLD", "TMPLTE", "TEMPLATE", "BASEOS"]


def _is_excluded_template(hostname: str) -> bool:
    if not hostname:
        return False
    h = hostname.upper()
    return any(keyword in h for keyword in EXCLUDED_HOSTNAME_KEYWORDS)


def _epoch_millis_to_iso(value) -> str | None:
    """EC returns timestamps as epoch milliseconds (e.g. 1784310467000).
    -1 means 'no timestamp' / not applicable — return None rather than
    a bogus 1969 date."""
    if value is None or value == -1 or value == "--":
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return None


def _infer_site_and_type(hostname: str) -> tuple[str | None, str | None]:
    """Confirmed convention (from real hostname samples + user confirmation):

    Direct prefix (non-FTCI): first 2 letters = site code, then
    LT/LPT=laptop, DTC/DT=desktop, TAB=tablet, SVR=server, else server
    (catch-all).

    FTCI prefix: device type immediately follows "FTCI" (LT/LPT/DTC/DT/
    TAB/SVR, else server), then a site code (PLS/NCS/XSC/SCS/SLC/GDT)
    appears somewhere in what's left.

    EXCLUDED entirely (returns None, None, no catch-all applied): hostnames
    matching Windows' own default auto-generated naming (DESKTOP-XXXXXXX,
    PC-XXXXXXXX) — un-renamed client machines that never got assigned into
    this naming convention at all. Confirmed policy: leave these
    unclassified and flag for a manual rename/cleanup pass rather than
    guess desktop/laptop for them."""
    if not hostname:
        return None, None
    h = hostname.upper()

    if h.startswith("DESKTOP-") or h.startswith("PC-"):
        return None, None

    if h.startswith("FTCI"):
        rest = h[4:]
        device_type = None
        remainder = rest
        if rest.startswith("SVR"):
            device_type = "server"
            remainder = rest[3:]
        elif rest.startswith("TAB"):
            device_type = "tablet"
            remainder = rest[3:]
        elif rest.startswith("DTC"):
            device_type = "desktop"
            remainder = rest[3:]
        elif rest.startswith("LPT"):
            device_type = "laptop"
            remainder = rest[3:]
        elif rest.startswith("DT"):
            device_type = "desktop"
            remainder = rest[2:]
        elif rest.startswith("LT"):
            device_type = "laptop"
            remainder = rest[2:]
        else:
            # Confirmed catch-all: FTCI-prefixed hosts that don't match any
            # known laptop/desktop/tablet/server token (e.g. FTCIDHCP001,
            # FTCIDASH) are still servers — infrastructure/app servers named
            # after their role rather than the LT/DTC/TAB/SVR convention.
            device_type = "server"

        site = None
        for code, name in FTCI_SITE_CODES.items():
            if code in remainder:
                site = name
                break
        return site, device_type

    site_code = h[:2]
    site = DIRECT_SITE_CODES.get(site_code)
    rest = h[2:]
    device_type = None
    if rest.startswith("SVR"):
        device_type = "server"
    elif rest.startswith("TAB"):
        device_type = "tablet"
    elif rest.startswith("DTC"):
        device_type = "desktop"
    elif rest.startswith("LPT"):
        device_type = "laptop"
    elif rest.startswith("DT"):
        device_type = "desktop"
    elif rest.startswith("LT"):
        device_type = "laptop"
    else:
        # Confirmed catch-all: anything not matching LT/LPT/DTC/DT/TAB/SVR
        # is still a server — covers named hosts with no site prefix at all
        # (e.g. AXSHAREPOINT) as well as recognized-site hosts with an
        # unfamiliar role abbreviation. Site stays whatever
        # DIRECT_SITE_CODES resolved to above (None if the prefix wasn't
        # recognized either).
        device_type = "server"

    return site, device_type


def _infer_agent_status(computer: dict) -> str:
    install_status = computer.get("installation_status")
    live_status = computer.get("computer_live_status")
    last_contact_iso = _epoch_millis_to_iso(computer.get("agent_last_contact_time"))

    if install_status != INSTALL_STATUS_INSTALLED:
        return "unmanaged"

    if last_contact_iso is None:
        return "unmanaged"

    last_contact_dt = datetime.fromisoformat(last_contact_iso)
    days_since_contact = (datetime.now(timezone.utc) - last_contact_dt).days

    if live_status == LIVE_STATUS_LIVE and days_since_contact <= STALE_AFTER_DAYS:
        return "healthy"
    if days_since_contact > OFFLINE_AFTER_DAYS:
        return "offline"
    if live_status == LIVE_STATUS_DOWN or days_since_contact > STALE_AFTER_DAYS:
        return "stale"
    return "healthy"


def _map_computer_to_device_row(computer: dict) -> tuple:
    hostname = computer.get("resource_name") or computer.get("full_name")
    site, device_type = _infer_site_and_type(hostname)

    if site is None:
        logger.warning(
            "Could not infer site for hostname %r (resource_id=%s) — "
            "left NULL, check naming convention",
            hostname, computer.get("resource_id"),
        )
    if device_type is None:
        logger.warning(
            "Could not infer device_type for hostname %r (resource_id=%s) — "
            "left NULL, check naming convention",
            hostname, computer.get("resource_id"),
        )

    return (
        str(computer.get("resource_id")),
        hostname,
        None,  # asset_tag — not present in this API; see module docstring KNOWN GAPS #1
        site,
        None,  # department — not present in this API; see module docstring KNOWN GAPS #2
        device_type,
        computer.get("os_name"),
        _epoch_millis_to_iso(computer.get("agent_last_contact_time")),
        _infer_agent_status(computer),
    )


def sync_devices() -> int:
    if not EC_SYNC_DB_CONNECTION_STRING:
        raise RuntimeError("EC_SYNC_DB_CONNECTION_STRING is not set.")

    session = get_session()
    conn = pyodbc.connect(EC_SYNC_DB_CONNECTION_STRING, timeout=10)
    cursor = conn.cursor()

    total = 0
    skipped_templates = 0
    try:
        computers = get_all_computers(session)
        for computer in computers:
            hostname = computer.get("resource_name") or computer.get("full_name")
            if _is_excluded_template(hostname):
                logger.info(
                    "Skipping golden-image/template machine: %r (resource_id=%s)",
                    hostname, computer.get("resource_id"),
                )
                skipped_templates += 1
                continue
            row = _map_computer_to_device_row(computer)
            cursor.execute(MERGE_SQL, row)
            total += 1
        conn.commit()
        logger.info(
            "Sync complete: %s devices upserted, %s templates skipped",
            total, skipped_templates,
        )
    finally:
        conn.close()

    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        sync_devices()
        sys.exit(0)
    except Exception:
        logger.exception("EC sync failed")
        sys.exit(1)

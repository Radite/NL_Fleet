"""
Generates a demo SQLite database with realistic fleet data so the whole
pipeline can be tried end-to-end without touching your real EC/ServiceDesk
Plus/TechDirect systems.

Run: python data/seed_db.py
"""

import random
import sqlite3
import string
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "fleet_demo.db"

SITES = ["Provo", "Grand Turk", "North Caicos", "Head Office"]
DEPARTMENTS = ["IT", "Finance", "Operations", "Customer Service", "Engineering", "HR"]
OS_VERSIONS = ["Windows 11 23H2", "Windows 11 24H2", "Windows 10 22H2"]
DEVICE_TYPES = ["laptop", "desktop", "server", "printer"]
PRINTER_MODELS = ["HP LaserJet M428", "Brother HL-L6300DW", "Canon imageRUNNER 2625"]

random.seed(42)  # reproducible demo data


def random_date(start_days_ago, end_days_ago):
    days = random.randint(end_days_ago, start_days_ago)
    return (datetime.now() - timedelta(days=days)).date().isoformat()


def random_datetime_recent(max_days_ago):
    days = random.randint(0, max_days_ago)
    hours = random.randint(0, 23)
    return (datetime.now() - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def build_devices(n=350):
    rows = []
    for i in range(1, n + 1):
        site = random.choice(SITES)
        dept = random.choice(DEPARTMENTS)
        device_type = random.choices(DEVICE_TYPES, weights=[45, 40, 5, 10])[0]
        hostname_prefix = {"laptop": "LT", "desktop": "WS", "server": "SRV", "printer": "PRT"}[device_type]
        hostname = f"{site[:2].upper()}-{hostname_prefix}-{i:04d}"
        asset_tag = "".join(random.choices(string.ascii_uppercase + string.digits, k=7))

        purchase_date = random_date(1460, 30)  # up to 4 years ago
        # warranty roughly 3 years from purchase, some already expired
        purchase_dt = datetime.fromisoformat(purchase_date)
        warranty_end = (purchase_dt + timedelta(days=365 * 3)).date().isoformat()

        # simulate agent health issues — the real audit found significant
        # issues here, so weight the demo data the same way
        agent_status = random.choices(
            ["healthy", "stale", "offline", "unmanaged"],
            weights=[55, 20, 15, 10],
        )[0]

        if agent_status == "healthy":
            last_checkin = random_datetime_recent(2)
        elif agent_status == "stale":
            last_checkin = random_datetime_recent(45)
        elif agent_status == "offline":
            last_checkin = random_datetime_recent(120)
        else:  # unmanaged
            last_checkin = random_date(400, 200)

        rows.append((
            f"DEV-{i:05d}", hostname, asset_tag, site, dept, device_type,
            random.choice(OS_VERSIONS) if device_type != "printer" else None,
            last_checkin, agent_status, purchase_date, warranty_end,
        ))
    return rows


def build_patch_compliance(device_rows):
    rows = []
    scan_date = datetime.now().isoformat(timespec="seconds")
    for device in device_rows:
        device_id, _, _, _, _, device_type = device[0], *device[1:5], device[5]
        if device_type == "printer":
            continue
        missing_critical = random.choices([0, 1, 2, 3, 5], weights=[50, 20, 15, 10, 5])[0]
        missing_total = missing_critical + random.randint(0, 8)
        rows.append((
            device_id, scan_date, missing_critical, missing_total, missing_critical == 0
        ))
    return rows


def build_warranty_lookup(device_rows):
    rows = []
    refreshed = datetime.now().isoformat(timespec="seconds")
    for device in device_rows:
        asset_tag = device[2]
        warranty_end = datetime.fromisoformat(device[10])
        days_remaining = (warranty_end - datetime.now()).days
        if days_remaining < 0:
            status = "Expired"
        elif days_remaining <= 90:
            status = "Expiring Soon"
        else:
            status = "Active"
        rows.append((asset_tag, status, days_remaining, refreshed))
    return rows


def build_printers(n=35):
    rows = []
    for i in range(1, n + 1):
        rows.append((
            f"PRN-{i:04d}",
            random.choice(SITES),
            random.choice(DEPARTMENTS),
            random.choice(PRINTER_MODELS),
            random.randint(5, 100),
            random_datetime_recent(10),
            random.choices(["online", "offline", "error"], weights=[80, 12, 8])[0],
        ))
    return rows


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE devices (
            device_id TEXT PRIMARY KEY,
            hostname TEXT,
            asset_tag TEXT,
            site TEXT,
            department TEXT,
            device_type TEXT,
            os_version TEXT,
            last_checkin TEXT,
            agent_status TEXT,
            purchase_date TEXT,
            warranty_end_date TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE patch_compliance (
            device_id TEXT,
            scan_date TEXT,
            missing_critical_patches INTEGER,
            missing_total_patches INTEGER,
            compliant BOOLEAN
        )
    """)
    cur.execute("""
        CREATE TABLE warranty_lookup (
            asset_tag TEXT,
            warranty_status TEXT,
            days_remaining INTEGER,
            last_refreshed TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE printers (
            printer_id TEXT PRIMARY KEY,
            site TEXT,
            department TEXT,
            model TEXT,
            toner_level_percent INTEGER,
            last_seen TEXT,
            status TEXT
        )
    """)

    devices = build_devices()
    cur.executemany("INSERT INTO devices VALUES (?,?,?,?,?,?,?,?,?,?,?)", devices)
    cur.executemany("INSERT INTO patch_compliance VALUES (?,?,?,?,?)", build_patch_compliance(devices))
    cur.executemany("INSERT INTO warranty_lookup VALUES (?,?,?,?)", build_warranty_lookup(devices))
    cur.executemany("INSERT INTO printers VALUES (?,?,?,?,?,?,?)", build_printers())

    conn.commit()
    conn.close()
    print(f"Seeded {DB_PATH} with {len(devices)} devices, {len(devices)} patch/warranty rows, 35 printers.")


if __name__ == "__main__":
    main()

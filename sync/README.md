# EC (Endpoint Central) → devices table sync

Pulls computer inventory from ManageEngine Endpoint Central and upserts it
into the `devices` table that the fleet NL query app reads. Runs as a
separate scheduled job — it is NOT part of the FastAPI app in `app/`, and
never will be; keeping ingestion and query serving separate means a slow or
failing EC API call can't take down the interactive query tool.

## Before you trust this with anything

Every field-name lookup in `ec_client.py` and `sync_devices.py` is marked
`--- VERIFY AGAINST YOUR INSTANCE ---`. I don't have access to your EC
server, so I wrote these against ManageEngine's general documented API
shape, not a tested response from your actual instance. **Do this first:**

```bash
export EC_BASE_URL=https://<your-ec-server>:8383
export EC_USERNAME=<a service account with API + Inventory access>
export EC_PASSWORD=<its password>

python3 -c "
from sync.ec_client import authenticate, get_computers
session = authenticate()
computers = get_computers(session, page=1, page_size=1)
import json
print(json.dumps(computers, indent=2))
"
```

Look at the printed JSON. Compare every key against what
`_map_computer_to_device_row()` in `sync_devices.py` assumes
(`resource_id`, `computer_name`, `service_tag`, `branch_office_name`, etc.)
and fix any that don't match. Do the same for the `authenticate()` token
lookup if that call fails first.

**Also confirm cloud vs. on-prem** — this was written for on-prem EC
(username/password → token). Cloud EC uses OAuth 2.0 via Zoho's identity
platform instead; if that's you, the entire `authenticate()` function needs
replacing with an OAuth flow, not just field-name tweaks.

## Setting up the write-side database login

The query app's `fleet_nl_reader` login must stay read-only — that's the
real safety boundary for the whole NL→SQL pipeline (see the main README).
This sync job needs to write, so give it a **separate, narrowly-scoped**
login instead of reusing or upgrading the reader login:

```sql
CREATE LOGIN fleet_ec_sync WITH PASSWORD = '<generate a strong password>';
CREATE USER fleet_ec_sync FOR LOGIN fleet_ec_sync;
GRANT SELECT, INSERT, UPDATE ON dbo.devices TO fleet_ec_sync;
-- Deliberately no DELETE grant — see sync_devices.py's docstring for why.
```

## Running it

```bash
pip install -r requirements-prod.txt -r sync/requirements.txt --break-system-packages
export EC_SYNC_DB_CONNECTION_STRING="Driver={ODBC Driver 18 for SQL Server};Server=tcp:<server>,1433;Database=<db>;Uid=fleet_ec_sync;Pwd=<password>;Encrypt=yes;"
python -m sync.sync_devices
```

## Scheduling it in production

Pick one, matched to your hosting:

- **Azure Function, Timer trigger** — cleanest fit if you're already on
  Azure App Service for the query app. Wrap `sync_devices()` in a Timer
  trigger function, put the same secrets in the same Key Vault, run every
  15-60 minutes.
- **cron** (if this runs on a Linux box you control): 
  `*/30 * * * * /path/to/venv/bin/python -m sync.sync_devices >> /var/log/ec_sync.log 2>&1`
- **Windows Task Scheduler** (if EC itself is on-prem Windows and you'd
  rather run the sync job on the same network segment): a basic task
  running `python -m sync.sync_devices` on your chosen interval.

Whichever you pick, alert on job failure (non-zero exit code) — a silently
failing sync job means the query app starts answering questions against
increasingly stale data without anyone noticing, which is worse than the
job not existing at all.

## What this does NOT do

- **No warranty data.** `purchase_date` and `warranty_end_date` on
  `devices`, and the whole `warranty_lookup` table, come from the separate
  TechDirect job `schema.py` already assumes exists. This job doesn't
  touch those columns.
- **No ServiceDesk Plus data.** If `patch_compliance` needs its own live
  source instead of a separate existing process, that's a third sync job
  following this same pattern — different API, same shape (auth → paginate
  → map → upsert with its own scoped write login).
- **No printer data.** `printers` is described in `schema.py` as coming
  from "SNMP monitoring scripts" — again, a separate existing process this
  job doesn't replace.

# Fleet NL Query Tool

Ask plain-language questions about the fleet ("which devices are coming up
on warranty expiration soon?") and get back the exact SQL that answered it,
alongside the results — never a bare sentence with no traceable query
behind it.

Built for a small, well-understood set of read-only tables (devices, patch
compliance, warranty lookups, printers). Ships as a working demo against a
seeded SQLite database so you can try the whole pipeline before wiring it
to real EC/ServiceDesk Plus/TechDirect data.

## Quickstart

```bash
pip install -r requirements.txt --break-system-packages
python data/seed_db.py          # generates data/fleet_demo.db with ~350 demo devices
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 — try the example chips, or type your own
question.

**Without an API key**, the app runs in mock mode: `app/claude_client.py`
pattern-matches a handful of demo questions (warranty, stale check-ins,
count-by-site, low toner, delete attempts) so you can test the full
pipeline offline. Set `ANTHROPIC_API_KEY` in your environment to switch to
real model calls — no code changes needed.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload --port 8000
```

### Optional: require an API key

By default `/ask` has no authentication — fine for trying this out on your
own machine, not fine for anything reachable by anyone else. Set
`APP_API_KEY` to require a shared-secret header on every request:

```bash
export APP_API_KEY=some-long-random-string
uvicorn app.main:app --reload --port 8000
```

The bundled UI has an "API key" button in the header that stores the key in
the browser's `localStorage` and sends it as `X-API-Key` on every request.
This is a stopgap for a small internal tool, not a substitute for real SSO
— see "What I'd still want" below.

### Optional: tune the rate limit

`APP_RATE_LIMIT` (default `20`) caps requests per client IP per 60-second
window, enforced in-process. If you ever run this behind a load balancer
with multiple workers, move this to Redis (or push it to the reverse proxy)
— an in-process dict isn't shared across workers.

## Deploying to production (Azure App Service + SQL Server + Entra ID)

This assumes: your real reconciliation data lives in **SQL Server**, you're
deploying to **Azure App Service**, and you're using **Entra ID** (you
already have Entra ID P2) for real sign-in instead of the shared API key.
If any of those three don't match your environment, the shape of these
steps still applies — swap the specific commands for your DB engine/host.

### 1. Create the read-only SQL Server login

Run this against your production reconciliation database (adjust the
table/view names to match your actual schema):

```sql
CREATE LOGIN fleet_nl_reader WITH PASSWORD = '<generate a strong password>';
CREATE USER fleet_nl_reader FOR LOGIN fleet_nl_reader;
GRANT SELECT ON dbo.devices TO fleet_nl_reader;
GRANT SELECT ON dbo.patch_compliance TO fleet_nl_reader;
GRANT SELECT ON dbo.warranty_lookup TO fleet_nl_reader;
GRANT SELECT ON dbo.printers TO fleet_nl_reader;
-- No other grants — do not add db_datareader at the database level.
```

**Verify it before moving on** — this login is the actual security
boundary now (not an OS-level flag like the SQLite demo), so prove it:

```bash
python3 -c "
import pyodbc
conn = pyodbc.connect('<your connection string using fleet_nl_reader>')
conn.execute('DELETE FROM devices')
"
# Expect: pyodbc.Error — permission denied. If this succeeds, STOP —
# the login has write access and nothing else here is safe until fixed.
```

### 2. Create a Key Vault and store the secrets

```bash
az keyvault create --name fleet-nl-kv --resource-group <your-rg> --location <region>

az keyvault secret set --vault-name fleet-nl-kv --name db-connection-string \
  --value "Driver={ODBC Driver 18 for SQL Server};Server=tcp:<your-server>,1433;Database=<your-db>;Uid=fleet_nl_reader;Pwd=<the password from step 1>;Encrypt=yes;"

az keyvault secret set --vault-name fleet-nl-kv --name anthropic-api-key \
  --value "sk-ant-..."
```

### 3. Create the App Service (as a container — see why below)

```bash
az group create --name <your-rg> --location <region>

az appservice plan create --name fleet-nl-plan --resource-group <your-rg> \
  --sku B1 --is-linux

az webapp create --name fleet-nl-query --resource-group <your-rg> \
  --plan fleet-nl-plan --deployment-container-image-name mcr.microsoft.com/appsvc/staticsite:latest
# (placeholder image — step 6 pushes the real one)
```

Using **Web App for Containers** rather than App Service's plain Python
runtime is deliberate: `pyodbc` needs the Microsoft ODBC Driver for SQL
Server installed at the OS level, which the plain runtime doesn't have.
The `Dockerfile` in this repo installs it.

### 4. Enable managed identity and grant it Key Vault access

```bash
az webapp identity assign --name fleet-nl-query --resource-group <your-rg>
# note the returned principalId

az keyvault set-policy --name fleet-nl-kv --object-id <principalId> \
  --secret-permissions get list
```

### 5. Configure app settings (Key Vault references — no secrets in plain env vars)

```bash
az webapp config appsettings set --name fleet-nl-query --resource-group <your-rg> --settings \
  DB_BACKEND=sqlserver \
  DB_CONNECTION_STRING="@Microsoft.KeyVault(SecretUri=https://fleet-nl-kv.vault.azure.net/secrets/db-connection-string/)" \
  ANTHROPIC_API_KEY="@Microsoft.KeyVault(SecretUri=https://fleet-nl-kv.vault.azure.net/secrets/anthropic-api-key/)" \
  APP_RATE_LIMIT=20
```

Note there's no `APP_API_KEY` here — Entra ID (step 6) is the real gate in
production; the shared-key path in the code is only a local-dev fallback.

### 6. Build and deploy the container

```bash
az acr create --name fleetnlacr --resource-group <your-rg> --sku Basic
az acr login --name fleetnlacr

docker build -t fleetnlacr.azurecr.io/fleet-nl-query:latest .
docker push fleetnlacr.azurecr.io/fleet-nl-query:latest

az webapp config container set --name fleet-nl-query --resource-group <your-rg> \
  --docker-custom-image-name fleetnlacr.azurecr.io/fleet-nl-query:latest
```

### 7. Turn on Entra ID authentication (Easy Auth)

In the Azure Portal: **App Service → Authentication → Add identity provider
→ Microsoft**. Either create a new Entra ID app registration or pick an
existing one. Set:

- **Restrict access:** Require authentication
- **Unauthenticated requests:** HTTP 302 redirect to identity provider
  (this app is a browser UI, so a redirect, not a bare 401, is what you want)

Then, to actually restrict *who* can sign in (this is the real access
control, not just "signed in with any Microsoft account"): go to **Entra ID
→ Enterprise Applications → (your app) → Properties → set "Assignment
required?" to Yes**, then **Users and groups → add** the specific
IT/ops group who should have access.

No code change is needed for this — `main.py` already reads the
`X-MS-CLIENT-PRINCIPAL-NAME` header App Service injects once a user is
authenticated, and uses it for the audit log instead of a bare IP address.

### 8. Smoke-test it

```bash
curl https://fleet-nl-query.azurewebsites.net/
# should redirect to Microsoft login if you're not already signed in
```

Sign in through the browser at that URL, ask a real question, and confirm:
the SQL shown matches your real schema, the row counts look sane, and
`az webapp log tail --name fleet-nl-query --resource-group <your-rg>` shows
your actual UPN (not an IP) in the audit log line.

### 9. Before calling it done

- Run the 20-30 real-question test pass mentioned below — mock-mode
  keyword matching and the real Claude API behave very differently, and you
  won't have caught schema-specific phrasing issues yet.
- Turn on Application Insights (`az monitor app-insights component create`)
  for durable log retention and error alerting — right now logs only go to
  stdout, which App Service captures but doesn't retain indefinitely.
- Keep the App Service Plan at a single instance for now (no autoscale) —
  the in-process rate limiter's request log isn't shared across instances,
  so scaling out silently weakens it. If you need to scale out later, move
  the counter to Redis (Azure Cache for Redis) first.
- Set a budget alert on the Anthropic API spend and on the App Service
  plan — an LLM-backed chat endpoint is the easiest thing in this stack to
  accidentally leave with no cost ceiling.

## Running the tests


```bash
python -m pytest tests/ -v
```

37 tests covering: valid query shapes, every forbidden statement type
(DELETE/DROP/UPDATE/INSERT/ALTER/TRUNCATE/CREATE), stacked-query injection,
unknown-table references, missing LIMIT clauses, over-limit requests,
comment-smuggled keywords, string-literal-smuggled keywords, request-level
auth (both the shared-key fallback and the Easy Auth header path), rate
limiting, question length limits, and the sqlite/sqlserver backend switch.
Also verifies the few-shot examples in the system prompt actually pass the
validator — a prompt teaching the model to produce SQL that then gets
rejected is worse than no few-shot examples at all, so this is checked in
CI rather than trusted by inspection.

## Architecture

```
app/
  schema.py         Curated table/column definitions — the ONLY thing the model can see
  prompts.py         System prompt + few-shot examples for NL -> SQL
  claude_client.py   API wrapper (real mode + offline mock mode)
  validator.py        App-level allow-list check (defense-in-depth, not the boundary)
  db.py               sqlite (demo) or sqlserver (prod) — selected via DB_BACKEND
  main.py              FastAPI orchestration: auth -> rate limit -> prompt -> validate -> execute -> respond
static/index.html      Single-page console UI (incl. optional API key entry for non-Azure use)
data/seed_db.py         Generates realistic demo data (sqlite backend only)
tests/                  Validator + full-pipeline + backend-switch tests
Dockerfile              Production image (installs the SQL Server ODBC driver)
requirements-prod.txt   Extra deps for DB_BACKEND=sqlserver (kept out of the base requirements)
```

## Security model — read this before pointing it at real data

There are two layers, and **only one of them is the actual boundary**:

1. **`validator.py`** — app-level regex/token checks. Rejects non-SELECT
   statements, stacked queries, unknown tables, missing LIMIT clauses,
   over-limit requests. This catches obvious cases and gives good error
   messages, but a determined prompt injection (e.g. text smuggled into a
   query via a hostname or department field the model later echoes) could
   in principle produce something that slips past a regex check.

   **2026-07 fix:** the regex checks used to run against the raw SQL
   string, which meant a query like
   `SELECT * FROM devices WHERE notes = 'see LIMIT 999999 in ticket'`
   could satisfy the `LIMIT` regex from text *inside a string literal* and
   sail through with no real row cap — a fail-open bug. The validator now
   sanitizes string-literal contents (via the sqlparse token stream) before
   running the LIMIT check and table-name extraction, so a data value can no
   longer be mistaken for SQL syntax. Comments are deliberately left alone —
   a comment is still treated as an unsafe place to hide a forbidden
   keyword, and there's a regression test guarding that behavior
   specifically (`test_comment_smuggled_ddl_still_caught_by_keyword_scan`).

2. **`db.py`** — the real boundary. The demo uses SQLite opened with the
   `mode=ro` URI flag, which makes the OS-level file handle read-only. I
   proved this actually works, not just in theory:

   ```
   $ python3 -c "from app.db import get_readonly_connection; \
       get_readonly_connection().execute('DELETE FROM devices')"
   OperationalError: attempt to write a readonly database
   ```

   That's a real SQLite-level rejection, independent of anything the
   validator caught. **Always run both layers — never one instead of the
   other.**

3. **`main.py`** — request-level controls added in the 2026-07 hardening
   pass: optional shared-secret auth (`APP_API_KEY`), a per-IP in-memory
   rate limit (`APP_RATE_LIMIT`), a hard cap on question length (500 chars)
   so a huge input can't inflate prompt cost, and DB execution errors are
   now logged in full server-side but returned to the client as a generic
   message — the previous version echoed the raw exception (including the
   local DB file path) back in the API response.

### Moving this to your real database

See "Deploying to production" below for the full walkthrough (SQL Server
login creation, Key Vault, App Service, Entra ID). Postgres users: the
`db.py` backend switch currently only implements `sqlite` and `sqlserver`
— add a `_get_postgres_connection()` following the same pattern (psycopg2,
same `DB_CONNECTION_STRING` env var convention) if that's your engine
instead.

## On using Copilot instead of Claude

Investigated during review: the org's Microsoft 365 Copilot Business
license does **not** provide a drop-in equivalent of the Anthropic API used
in `claude_client.py`. The available Microsoft 365 Copilot APIs
(`/copilot/` under Graph) require a signed-in, individually-licensed user
authenticating via delegated Entra ID OAuth for every call, and are built
around grounding responses in that user's M365 data — not a clean
"system prompt in, JSON out" contract. Adopting it would mean building a
full Entra ID sign-in flow into an app that currently has none, and fighting
the grounding behavior to get plain instruction-following output. If cost
is the actual driver rather than the "Copilot" branding specifically, check
whether the org has a separate **Azure OpenAI** resource — that would be a
much closer drop-in swap for `claude_client.py` (same shape: system prompt
+ message in, JSON out).

## What I'd still want before handing this to Bradley or Jerry

- **Named owner after you leave.** This has a prompt-tuning dependency —
  if the schema changes and nobody updates `schema.py`, the tool starts
  giving confidently wrong answers rather than failing loudly.
- **Real question testing.** The mock mode only recognizes a handful of
  patterns. Before going live, run 20-30 actual questions people would ask
  through the real API and see where phrasing trips it up — this is
  usually where estimates blow up, budget real time for it.
- **Watch for query-reading fatigue.** People stop reading the generated
  SQL after the third correct answer and start trusting it blindly. The
  "verified read-only" stamp in the UI is a nudge, not a fix for that.
- **Fleet size check.** At ~350-400 endpoints, make sure the query patterns
  people actually want are varied enough to justify an LLM layer over just
  adding more filters to a dashboard — this adds real maintenance surface
  for a fleet this size.
- **Real SSO if this leaves your laptop.** `APP_API_KEY` is a single shared
  secret for everyone — fine for a handful of trusted people on a private
  network, not an access-control system. If more than a few people will use
  this, or if you need per-user audit trails, put it behind your real
  identity provider instead.
- **Multi-worker deployment.** The rate limiter and its request log live in
  a single process's memory. If you ever run this with more than one
  uvicorn worker or instance, that limit stops being enforced consistently
  — move it to Redis or your reverse proxy at that point.
"# NL_Fleet" 

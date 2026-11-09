# InsureRemind — Azure deployment plan

**Target:** Azure App Service (Linux, Python 3.12) + Azure Database for PostgreSQL Flexible Server + Azure Blob Storage,
all in the **Central India (Pune)** region.

**Status:** Phase 1 implemented on branch `azure-phase1` (not yet merged); Phases 2–5 pending.

| Phase | What | Who | Estimated time |
|---|---|---|---|
| 1 | Code changes (storage, gunicorn, static files, reminder trigger, health check, upload hardening, audit log, telemetry) | Developer (Claude) | 1.5 days |
| 2 | Create the Azure resources | Agency owner, using the checklist | 2–3 hours |
| 3 | First deploy and smoke test on `*.azurewebsites.net` | Both | 1–2 hours |
| 4 | Data migration from Supabase and local `media/`, then switch to the custom domain | Both | 1 evening |
| 5 | After go-live: hardening, monitoring, backup restore test | Agency owner | 1–2 hours |

---

## 1. Target architecture

```
 Users (7–8, any location)
        │  HTTPS (managed certificate, custom domain)
        ▼
 ┌──────────────────────────────────────────────┐
 │ Azure App Service — Linux B1, Python 3.12    │
 │   gunicorn (2 workers × 4 threads, 180 s)    │
 │   Django + WhiteNoise (static files)         │
 │   System-assigned managed identity ──────────┼──► Blob Storage (private container "documents")
 │   App settings / Key Vault references        │      versioning + soft delete, uuid blob names
 │   OpenTelemetry ─────────────────────────────┼──► Application Insights (requests, exceptions)
 └───────────────┬──────────────────────────────┘
                 │ TLS (sslmode=require)
                 ▼
 PostgreSQL Flexible Server — Burstable B1ms, PG 16, 32 GB, 7-day backups
   database "insureremind", role "insureremind_app", schema "insureremind"

 Logic App (Consumption) — every day at 09:00 IST
   POST https://<app>/internal/cron/send-reminders/  (X-Cron-Token header) ──► runs send_reminders
```

### Estimated monthly cost

| Resource | SKU | Approx. USD per month |
|---|---|---|
| App Service plan | Linux **B1** (1 core, 1.75 GB RAM, Always On) | ~$13 |
| PostgreSQL Flexible Server | Burstable **B1ms** (1 vCore, 2 GB) + 32 GB storage, 7-day backups included | ~$16 |
| Storage account | Standard, LRS, Hot tier, ~7–20 GB | < $1 |
| Logic App (Consumption) | 1 run a day | ~$0 |
| Key Vault (optional) | Standard, a few secret reads | < $0.10 |
| Application Insights | Workspace-based, well under the 5 GB/month free allowance | ~$0 |
| Managed certificate for the custom domain | Free | $0 |
| **Total** | | **~$30 (≈ ₹2,650)** |

> Prices are list estimates. Confirm them in the Azure Pricing Calculator for **Central India** before creating
> resources. Set a budget alert (Phase 2, step 1).

---

## 2. Design decisions and why

| Topic | Decision | Reason |
|---|---|---|
| Hosting | App Service (PaaS), not a VM | No OS patching, Nginx or certbot. Deploys straight from git. Managed HTTPS. |
| Region | Central India | Keeps client data in India (DPDP Act); low latency for users in India. |
| Documents | Private Blob container through `django-storages[azure]` | The code already uses Django's storage API (`.open()` / `.save()`), so only settings change. Files are never public and are still served only through the login-protected `policy_document`, `upload_file` and ZIP views. |
| Storage authentication | **Managed identity** + RBAC role *Storage Blob Data Contributor* | No storage keys or connection strings in the app settings. |
| Database layout | Same as Supabase today: role `insureremind_app`, schema `insureremind`, `search_path` set on the role | `pg_dump` / `pg_restore` work 1:1. No change to Django settings except `DATABASE_URL`. |
| Database network | Public access with the firewall limited to App Service outbound IPs (+ your IP temporarily during migration), TLS enforced | Simplest setup that is still locked down. VNet / private access is an optional hardening step (§7). |
| Daily reminders | Logic App (Consumption) that POSTs to a token-protected internal URL | App Service has no built-in cron for Python apps. Logic Apps are set up in the portal at ~zero cost. `send_reminders` is already idempotent and uses `select_for_update(skip_locked=True)`, so a retry or a double call never double-sends. |
| Long AI extraction | gunicorn `--timeout 180`, threaded workers | Extraction runs inside the upload request with `timeout_seconds = 120` (`config/llm.toml`). App Service's front end allows 230 s per request, so it fits. Threads stop one slow upload from blocking other users. |
| Static files | WhiteNoise with compressed, hashed files | No separate static file host. Only Django admin and a few assets use static files. |
| Secrets | App Service app settings, ideally as **Key Vault references** | Secrets never go in git or a `.env` on the server. `python-dotenv` simply finds no `.env` and reads environment variables. |
| Deployment | GitHub Actions with OIDC (no publish profile secret) | Push to `main` → tests → deploy. Fallback: `az webapp up` or zip deploy from your PC. |
| Database engine | PostgreSQL Flexible Server, **not** Azure SQL | The reference approach allows either. Postgres keeps `psycopg`, every existing migration and a 1:1 `pg_dump` / `pg_restore` from Supabase. Azure SQL would need `mssql-django`, an ODBC driver and full re-testing. |
| Document delivery | Stream through the protected Django views, **no SAS URLs** | The reference approach suggests short-lived SAS URLs. Streaming keeps authorisation and auditing in one place: a SAS link works for anyone holding it until it expires and leaves no record of who opened it. The ZIP download and the friendly `REGNO_policyno_startdate` file names already need the server to handle the file. At 7–8 users the extra load is negligible. |
| Blob names | Random `uuid` per file (`uploads/<uuid>.<ext>`, `policies/<uuid>.<ext>`) | Agents often name files after the client or registration number. Keeping the original name would put personal data into blob names, Storage logs and listings. Download names come from `document_filename`, so users never see the uuid. |
| Upload validation | Check the file's leading bytes as well as the extension | The browser-reported `content_type` is set by the client and can't be trusted. |
| Audit and telemetry | `DocumentAccess` table in the app database, plus Application Insights via OpenTelemetry | Who viewed, downloaded or ZIPped which client's documents must be answerable (DPDP Act). A database table survives log retention limits and is easy to query and export. App Insights gives request traces and exceptions. |

---

## 3. Phase 1 — Code changes (no Azure account needed)

All changes are made on a branch, with the full test suite green and `makemigrations --check` reporting
"No changes detected". Two migrations are expected: a `policies` migration that changes `upload_to` on
`Policy.document` and `PolicyUpload.file` (Django records the callable; no data changes), and one that creates the
`DocumentAccess` audit table (§3.5).

### 3.1 Dependencies (`requirements.txt`)
- [x] `gunicorn>=23,<24`
- [x] `whitenoise>=6.7,<7`
- [x] `django-storages[azure]>=1.14,<2`
- [x] `azure-identity>=1.17,<2` (managed identity credential)
- [x] `azure-monitor-opentelemetry>=1.6,<2` (Application Insights)

### 3.2 Settings (`insureremind/settings.py`)
- [x] **Storage switch** by `STORAGE_BACKEND`:
  - `local` (default): current behaviour, `FileSystemStorage` on `MEDIA_ROOT`.
  - `azure`: `storages.backends.azure_storage.AzureStorage` with the following options:
    - `account_name` = `AZURE_STORAGE_ACCOUNT_NAME`
    - `azure_container` = `AZURE_STORAGE_CONTAINER` (default `documents`)
    - `token_credential` = `DefaultAzureCredential()` (managed identity on Azure, `az login` locally), or
      `connection_string` = `AZURE_STORAGE_CONNECTION_STRING` when set (local testing only)
    - `overwrite_files = False`, `expiration_secs = None` (no public SAS URLs are ever generated)
- [x] Keep `MEDIA_URL` as the non-served placeholder. Documents are only ever streamed through the protected views.
- [x] **Static files:** add `whitenoise.middleware.WhiteNoiseMiddleware` right after `SecurityMiddleware`, and set
      `STORAGES["staticfiles"]` to `whitenoise.storage.CompressedManifestStaticFilesStorage` (not used in tests).
- [x] `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` already come from the environment. Document that they must include
      `<app>.azurewebsites.net` and the custom domain.
- [x] `SECURE_PROXY_SSL_HEADER` is already set; App Service sends `X-Forwarded-Proto`. No change needed.
- [x] New setting `CRON_TOKEN = env("CRON_TOKEN")`. An empty value disables the cron endpoint (returns 404).

### 3.3 New endpoints (no login required, so they are exempt from `LoginRequiredMiddleware`)
- [x] `GET /healthz` → `200 ok` after a cheap `SELECT 1`. Used by App Service *Health check* and the uptime test.
      Decorated with `login_not_required`. Returns no data.
- [x] `POST /internal/cron/send-reminders/`:
  - `login_not_required`, `csrf_exempt`, POST only.
  - Requires header `X-Cron-Token`, compared with `secrets.compare_digest` against `CRON_TOKEN`. Returns 403 on
    mismatch and 404 when `CRON_TOKEN` is empty.
  - Runs `call_command("send_reminders")` and returns a short JSON summary (sent / failed / skipped counts).
  - Logs start, end and errors. Never returns reminder contents or recipients.
  - Optional `?dry_run=1` for testing from the Logic App.
- [x] Place both views in a small `ops` module (for example `insureremind/ops_views.py`) and wire them into `insureremind/urls.py`.

### 3.4 Upload hardening (`policies/models.py`, `policies/forms.py`)
- [x] **Random blob names.** Replace `upload_to="policies/"` and `upload_to="uploads/"` with module-level functions
      (for example `policy_document_path` and `upload_file_path`) that return `policies/<uuid4hex><ext>` and
      `uploads/<uuid4hex><ext>`. Keep only the lower-cased extension of the original name; drop the rest.
- [x] Existing files keep their stored names. No data migration and no renaming of blobs during Phase 4.
- [x] Check that nothing shows the stored file name to users. Single and ZIP downloads already use
      `document_filename`. If the review page displays the original upload name, store it in a field first
      (for example `PolicyUpload.original_name`) rather than reading it from the blob path.
- [x] **Content check.** In `validate_policy_file`, read the first 12 bytes (then `f.seek(0)`) and require them to
      match the extension:
  - `.pdf` → starts with `%PDF-`
  - `.jpg` / `.jpeg` → starts with `FF D8 FF`
  - `.png` → starts with `89 50 4E 47 0D 0A 1A 0A`
  - `.webp` → `RIFF` at bytes 0–3 and `WEBP` at bytes 8–11
  
  Error message: "The file content doesn't match its extension." Keep the extension, content-type and size checks.

### 3.5 Audit log and telemetry
- [x] **Model** `DocumentAccess` (in `policies/models.py`): `user` (FK, `PROTECT`, nullable for safety),
      `action` (`view` / `download` / `zip` / `upload_view`), `policy` (FK, nullable), `vehicle` (FK, nullable),
      `upload` (FK to `PolicyUpload`, nullable), `ip_address` (`GenericIPAddressField`, nullable), `at`
      (`auto_now_add`, indexed). Ordering `-at`. Ships with a migration.
- [x] **Recording:** one row per successful response in `policy_document` (`view`, or `download` when
      `?download=1`), `vehicle_documents_zip` (`zip`) and `upload_file` (`upload_view`). Don't record 403/404s as
      accesses. Take the client IP from the first `X-Forwarded-For` value (App Service puts the real IP there,
      sometimes with `:port`, which has to be stripped), falling back to `REMOTE_ADDR`.
- [x] Put the recording in one helper (for example `policies/audit.py: record_access(request, action, ...)`) so the
      views stay one line each. A failure to write the audit row is logged and must not break the download.
- [x] **Viewing:** register `DocumentAccess` in Django admin as read-only (no add / change / delete), with list
      filters for action and date and search by user and registration no. Only the Admin group can see it.
      Optional later: an Excel export on the reports page.
- [x] **Retention:** keep rows indefinitely for now (they are tiny). Revisit when a retention policy is agreed.
- [x] **Application Insights:** in `insureremind/wsgi.py` (not `settings.py`, so tests and management commands
      don't start it), call `azure.monitor.opentelemetry.configure_azure_monitor()` only when
      `APPLICATIONINSIGHTS_CONNECTION_STRING` is set. Set `OTEL_SERVICE_NAME=insureremind`.
- [x] **No personal data in telemetry:** don't add log messages containing names, phone numbers, emails or
      registration numbers. Report filters put registration numbers in the query string, so exclude query strings
      from collected request URLs (or exclude `/reports/` with `OTEL_PYTHON_DJANGO_EXCLUDED_URLS`), and check the
      first traces in the portal.

### 3.6 Deployment files (new `deploy/azure/` folder)
- [x] `startup.sh`: the App Service startup command
  ```bash
  #!/usr/bin/env bash
  set -e
  python manage.py migrate --noinput
  python manage.py collectstatic --noinput
  exec gunicorn insureremind.wsgi --bind=0.0.0.0:8000 \
       --workers 2 --threads 4 --worker-class gthread \
       --timeout 180 --graceful-timeout 30 \
       --access-logfile - --error-logfile -
  ```
  Running `migrate` on startup is safe here because the app runs as a **single** instance. Move it to the
  pipeline if you ever scale out.
- [x] `.github/workflows/azure-deploy.yml`: on push to `main`, set up Python 3.12, install requirements, run
      `python manage.py test` and `python manage.py makemigrations --check`, log in with `azure/login` (OIDC
      federated credential), then deploy with `azure/webapps-deploy`. App Service builds the dependencies itself
      because `SCM_DO_BUILD_DURING_DEPLOYMENT=true`.
- [x] `deploy/azure/logicapp-recurrence.json` (optional): an exported Logic App definition for the daily trigger.

### 3.7 Tests (`policies/tests.py`, `policies/test_reports.py`, new `insureremind/tests_ops.py`)
- [x] Fix `policies/test_reports.py:220` (`os.remove(self.old.document.path)`) to use
      `self.old.document.storage.delete(self.old.document.name)`. Remote storage has no `.path`.
- [x] Run the document, upload, confirm and ZIP tests once against `InMemoryStorage`
      (`override_settings(STORAGES=...)`) to prove nothing depends on the local filesystem.
- [x] Storage switch: `STORAGE_BACKEND=azure` builds the expected backend and options. Mock the credential;
      **never call Azure in tests**.
- [x] `/healthz`: 200 without login.
- [x] Cron endpoint: 404 when the token is unset; 403 for a wrong or missing token; 405 on GET; 200 with the right
      token, which sends due reminders; calling it twice doesn't double-send.
- [x] Blob names: an upload named `Client Name MH12AB1234.pdf` is stored as `uploads/<32 hex>.pdf`, and the
      confirmed policy as `policies/<32 hex>.pdf`; the download still uses `document_filename`.
- [x] Content check: a `.pdf` containing PNG bytes, a `.png` containing `%PDF-` and a `.jpg` of random bytes are
      rejected; valid samples of each of the four types pass. Use tiny dummy byte strings, not real files.
- [x] Audit: view, `?download=1`, ZIP and upload-file views each create exactly one `DocumentAccess` row with the
      right user and action; a 404 or a permission denial creates none; `X-Forwarded-For: 1.2.3.4:5678` is stored
      as `1.2.3.4`; a failing audit write still returns the file. The admin page is read-only.
- [x] Telemetry: `configure_azure_monitor` isn't called when the connection string is unset (mock it).
- [x] Keep the existing rules: mock `requests.post`, no real customer data, and never `.close()` a
      streaming/File response in tests.

### 3.8 Documentation
- [x] `.env.example`: add `STORAGE_BACKEND`, `AZURE_STORAGE_ACCOUNT_NAME`, `AZURE_STORAGE_CONTAINER`,
      `AZURE_STORAGE_CONNECTION_STRING` (local only), `CRON_TOKEN` and `APPLICATIONINSIGHTS_CONNECTION_STRING`
      (empty = telemetry off).
- [x] `README.md`: add the new variables to the configuration table, plus a short "Deploying to Azure" section
      linking to this file. Replace the "documents on local disk" note.
- [x] `CLAUDE.md`: update the Stack, Layout and Known gaps sections; add the `DocumentAccess` model to the domain
      model and the rule "every new document-serving view records a `DocumentAccess` row".

### 3.9 Phase 1 done when
- [x] `python manage.py test` is green on SQLite (111 tests).
- [ ] Test suite run once with `TEST_DATABASE_URL` on Postgres (needs credentials).
- [x] `python manage.py makemigrations --check` → "No changes detected".
- [x] `python manage.py check --deploy` (with production-like env) → only the known `security.W019`.
- [ ] Local run with `STORAGE_BACKEND=local` behaves exactly as today.

---

## 4. Phase 2 — Create the Azure resources (portal checklist)

Use one resource group so the whole system can be found, costed and deleted together.
Suggested names (must be globally unique where noted): resource group `rg-insureremind-prod`, app
`insureremind-<agency>` (unique), storage `stinsureremind<agency>` (unique, lowercase letters and numbers),
Postgres `psql-insureremind-<agency>` (unique).

1. **Account safety**
   - [ ] Enable MFA on the account that owns the subscription.
   - [ ] Cost Management → Budgets → create a **$50 per month** budget with email alerts at 50%, 80% and 100%.
2. **Resource group:** `rg-insureremind-prod`, region **Central India**.
3. **Storage account**
   - [ ] Standard performance, **LRS** redundancy, Hot access tier, region Central India.
   - [ ] Security: *Allow Blob anonymous access* = **Disabled**; *Allow storage account key access* = **Disabled**
         (managed identity only); minimum TLS 1.2.
   - [ ] Data protection: enable **blob soft delete (30 days)**, **container soft delete (30 days)** and **versioning**.
   - [ ] Lifecycle rule: delete previous versions older than 90 days.
   - [ ] Create a container named `documents`, access level **Private**.
4. **PostgreSQL Flexible Server**
   - [ ] Version **16** (or the same major version as Supabase, or newer), Burstable **B1ms**, 32 GB storage,
         storage auto-grow on.
   - [ ] Backups: 7-day retention, locally redundant.
   - [ ] Authentication: PostgreSQL authentication. Admin user `iradmin` with a long random password stored in
         your password manager.
   - [ ] Networking: **Public access**. Do **not** tick "Allow public access from any Azure service". Add
         firewall rules for your current IP (temporary) now; App Service IPs are added in step 6.
   - [ ] Server parameter `require_secure_transport` = ON (default).
   - [ ] Connect as `iradmin` (Azure Cloud Shell or `psql` from your PC) and run:
     ```sql
     create database insureremind;
     \c insureremind
     create role insureremind_app with login password '<strong-random-password>';
     grant insureremind_app to iradmin;          -- lets the admin hand over the schema
     create schema insureremind authorization insureremind_app;
     alter role insureremind_app set search_path = insureremind, public;
     revoke all on schema public from public;
     ```
5. **App Service**
   - [ ] Create a Web App: publish **Code**, runtime **Python 3.12**, OS **Linux**, region Central India,
         new App Service plan, Linux **B1**.
   - [ ] Configuration → General settings:
     - Startup command `bash deploy/azure/startup.sh`
     - **Always On** = On
     - **HTTPS Only** = On
     - Minimum TLS 1.2
     - FTP/FTPS = Disabled
   - [ ] Identity → System assigned → **On**.
   - [ ] Health check → path `/healthz`.
6. **Connect App Service to Postgres:** App Service → Networking → copy the **outbound IP addresses** (and
   "additional outbound IPs") → add each one as a firewall rule on the Postgres server.
7. **Give App Service access to Blob storage:** Storage account → Access control (IAM) → Add role assignment →
   **Storage Blob Data Contributor** → the web app's managed identity. Scope it to the storage account, or to the
   `documents` container only.
8. **Key Vault (recommended)**
   - [ ] Create `kv-insureremind-<agency>`, RBAC permission model. Give the web app's identity the
         **Key Vault Secrets User** role.
   - [ ] **Application Insights:** create `appi-insureremind` (workspace-based, Central India, new Log Analytics
         workspace `log-insureremind` with 30-day retention). Copy its **connection string** for step 9.
   - [ ] Add these secrets: `SECRET-KEY`, `DATABASE-URL`, `OPENROUTER-API-KEY`, `EMAIL-HOST-PASSWORD`,
         `WHATSAPP-ACCESS-TOKEN`, `CRON-TOKEN`.
9. **App settings** (App Service → Environment variables). Secrets use `@Microsoft.KeyVault(SecretUri=...)` references:

   | Name | Value |
   |---|---|
   | `DEBUG` | `false` |
   | `SECRET_KEY` | Key Vault reference (50+ random characters) |
   | `ALLOWED_HOSTS` | `insureremind-<agency>.azurewebsites.net,app.<yourdomain>.in` |
   | `CSRF_TRUSTED_ORIGINS` | `https://insureremind-<agency>.azurewebsites.net,https://app.<yourdomain>.in` |
   | `DATABASE_URL` | Key Vault ref → `postgres://insureremind_app:<pw>@psql-insureremind-<agency>.postgres.database.azure.com:5432/insureremind?sslmode=require` |
   | `STORAGE_BACKEND` | `azure` |
   | `AZURE_STORAGE_ACCOUNT_NAME` | `stinsureremind<agency>` |
   | `AZURE_STORAGE_CONTAINER` | `documents` |
   | `CRON_TOKEN` | Key Vault ref (long random string) |
   | `APPLICATIONINSIGHTS_CONNECTION_STRING` | From step 8 (not a secret, but can also be a Key Vault ref) |
   | `OTEL_SERVICE_NAME` | `insureremind` |
   | `OPENROUTER_API_KEY` | Key Vault reference |
   | `EMAIL_*`, `DEFAULT_FROM_EMAIL` | As today. Use port 587 + TLS (Azure blocks outbound port 25). |
   | `WHATSAPP_*` | As today (`WHATSAPP_ACCESS_TOKEN` as a Key Vault ref) |
   | `AGENCY_NAME`, `REMINDER_DAYS`, `AGENT_NOTIFY_EMAIL`, `AGENT_NOTIFY_PHONE` | As today |
   | `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |
   | `WEBSITES_PORT` | `8000` |
   | `TZ` | `Asia/Kolkata` (log timestamps only; Django already uses `TIME_ZONE`) |

10. **Deployment credentials (GitHub Actions)**
    - [ ] Microsoft Entra ID → App registrations → new app `gh-insureremind-deploy` → Certificates & secrets →
          **Federated credential** for your GitHub repo, branch `main`.
    - [ ] Give it the **Website Contributor** role on the web app.
    - [ ] Add the repo secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` (these IDs aren't
          passwords).
11. **Daily reminder trigger (Logic App, Consumption)**
    - [ ] Create `logic-insureremind-reminders` in the same resource group.
    - [ ] Trigger **Recurrence**: every 1 day, at 09:00, time zone **(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi**.
    - [ ] Action **HTTP**: `POST https://app.<yourdomain>.in/internal/cron/send-reminders/`, header
          `X-Cron-Token: <CRON_TOKEN>`. Retry policy: exponential, 3 retries (safe because sending is idempotent).
    - [ ] Store the token as a *secure input* (Settings → Secure inputs) so it doesn't appear in run history.
    - [ ] Add an email alert when a run fails (Logic App → Alerts → "Runs Failed > 0").

---

## 5. Phase 3 — First deploy and smoke test (before migrating real data)

- [ ] Push the Phase 1 branch to `main` (or run the workflow manually). Watch the GitHub Actions run, then
      App Service → **Log stream**.
- [ ] Open `https://insureremind-<agency>.azurewebsites.net/healthz` → `ok`.
- [ ] If App Service health check reports unhealthy, check the log stream for `DisallowedHost` — the probe's Host header must be in `ALLOWED_HOSTS`.
- [ ] App Service → SSH console: `python manage.py createsuperuser` (a temporary test admin).
- [ ] Log in; check that the CSS loads (WhiteNoise) and that `/admin/` works.
- [ ] Upload a **dummy** policy PDF. Extraction completes in under 2 minutes, the review page shows the document,
      and confirming works.
- [ ] Check that the file appears in Storage → `documents` → `uploads/` and `policies/` with a **uuid name** (no
      client name or registration no). Open the document, download it, and download the vehicle ZIP; the downloaded
      names are the friendly `REGNO_policyno_startdate` ones.
- [ ] Try uploading a `.txt` file renamed to `.pdf` → rejected with the content-mismatch message.
- [ ] `/admin/` → Document accesses: one row each for the view, download and ZIP above, with your user and IP.
- [ ] Application Insights → Transaction search: the requests above appear within a few minutes, and no
      registration numbers appear in the collected URLs.
- [ ] Logic App → *Run trigger* with `?dry_run=1` → 200 with a JSON summary. Then a real run → the reminder shows as
      sent in the app (console WhatsApp backend at first).
- [ ] Delete the dummy data and the temporary admin.

---

## 6. Phase 4 — Data migration and go-live

Plan a quiet evening. Tell the users the app is read-only from the start time.

### 6.1 Freeze
- [ ] Ask users to stop uploading. Note the time.

### 6.2 Database: Supabase → Azure
Run from your PC with PostgreSQL client tools whose version is **≥ the Supabase server version**:
```powershell
# Export only the app schema, without Supabase-specific owners or grants
pg_dump "postgres://insureremind_app.<ref>:<pw>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require" `
  --schema=insureremind --no-owner --no-privileges -Fc -f insureremind.dump

# Restore into Azure as the app role (the schema already exists and is owned by insureremind_app)
pg_restore --no-owner --role=insureremind_app --schema=insureremind `
  -d "postgres://insureremind_app:<pw>@psql-insureremind-<agency>.postgres.database.azure.com:5432/insureremind?sslmode=require" `
  insureremind.dump
```
- [ ] If the empty schema was already migrated during Phase 3, drop and recreate the `insureremind` schema first
      (as in step 4 of §4) so the restore doesn't conflict.
- [ ] Reset sequences if needed: `python manage.py sqlsequencereset accounts policies notifications auth | python manage.py dbshell`.
- [ ] Run `python manage.py migrate` against Azure. The Supabase dump predates Phase 1, so this applies the
      `upload_to` and `DocumentAccess` migrations. Then `python manage.py migrate --check` → no unapplied migrations.

### 6.3 Documents: local `media/` → Blob
Keep the same paths (`policies/...`, `uploads/...`) so the names stored in `FileField` still resolve. Files uploaded
before Phase 1 keep their original names; only new uploads get uuid names. (Renaming old blobs is optional and
can be done later with a one-off script that renames each blob and updates its `FileField`.)
```powershell
az login
az storage blob upload-batch --account-name stinsureremind<agency> --destination documents `
  --source .\media --auth-mode login --overwrite false
```
(Your own Azure user needs the *Storage Blob Data Contributor* role on the account for this step.)

### 6.4 Verify
- [ ] Row counts match between Supabase and Azure for `policies_owner`, `policies_vehicle`, `policies_policy`,
      `policies_policyupload`, `notifications_reminder` and `auth_user`.
- [ ] Number of blobs = number of files under `media/`.
- [ ] Open 5 random policy documents in the app, plus one vehicle ZIP and one Excel/PDF report.
- [ ] `send_reminders` dry run (Logic App `?dry_run=1`): the list of due reminders looks right.
- [ ] Every user can log in (passwords migrate with `auth_user`).

### 6.5 Custom domain and switch-over
- [ ] App Service → Custom domains → add `app.<yourdomain>.in`. Create the CNAME and TXT (`asuid`) records your
      DNS provider is asked for. Then add an **App Service Managed Certificate** and bind it (SNI SSL).
- [ ] Point the Logic App URL at the custom domain.
- [ ] Remove your PC's IP from the Postgres firewall.
- [ ] Tell the users the new URL.

### 6.6 Rollback plan
Supabase and the local `media/` folder are left **untouched**. If something is wrong, point the users back to the
old setup and stop the Logic App, so the two systems don't both send reminders. Keep Supabase read-only for
**2 weeks**, then export a final `pg_dump` for the archive and downgrade or delete the Supabase project.

> Only **one** system may run `send_reminders` at a time. Disable the old Task Scheduler or cron job on the day
> you switch.

---

## 7. Phase 5 — After go-live

- [ ] **Backup restore drill:** Postgres → Restore → point-in-time to a new server → open it with `psql` → delete
      the test server. Restore one soft-deleted blob.
- [ ] **Monitoring:**
  - Application Insights *availability test* on `/healthz` every 5 minutes, with email on failure
    (or a free external uptime monitor).
  - Metric alerts: App Service HTTP 5xx > 5 in 5 minutes; Postgres CPU > 80% for 15 minutes; Postgres storage > 80%.
  - Application Insights → Failures: review exceptions weekly for the first month.
  - Audit log: once a month, look through `/admin/` → Document accesses for unusual volume (for example, one user
    downloading many ZIPs).
- [ ] **Logs:** App Service → App Service logs → filesystem, 7-day retention. Use Log stream for live debugging.
- [ ] **WhatsApp:** switch `WHATSAPP_BACKEND=meta` and test one real template message (this has never been tested live).
- [ ] **Email (optional):** move to Azure Communication Services Email for better delivery from Azure IPs.
- [ ] **Hardening (optional, about $1 a month more):**
  - Move Postgres to VNet / private access.
  - Add App Service VNet integration.
  - Restrict the storage account to the VNet with a private endpoint.
- [ ] **Cost:** after a month of stable use, check Cost Management. Consider a 1-year reservation for App Service
      and Postgres (about 30–40% saving).
- [ ] **Privacy notice:** documents are stored in Azure Central India and sent to OpenRouter (and the model
      provider) for extraction. Mention both.

---

## 8. Risks and how they're handled

| Risk | Mitigation |
|---|---|
| AI extraction is slower than the request limit (230 s on App Service) | The OpenRouter timeout is 120 s and gunicorn's is 180 s. Failure degrades to manual entry (existing rule). Long term: move extraction to a background task. |
| One B1 instance: brief downtime during Azure platform maintenance or a restart | Acceptable for 7–8 internal users. Scale to 2 instances (≈ +$13) only if needed; then move `migrate` out of `startup.sh`. |
| App Service outbound IPs change (only when the plan's pricing tier changes) | Re-check the IPs after any tier change, or adopt VNet integration (§7). |
| Reminders sent twice (old and new scheduler both active) | Turn off the old scheduler at switch-over. `send_reminders` is idempotent and row-locked. |
| Cron URL abused | POST only, constant-time token check, 404 when unset, and running it is harmless (idempotent). Rotate `CRON_TOKEN` in Key Vault if it leaks. |
| Documents exposed publicly | Anonymous blob access disabled, key access disabled, managed identity only, and files served only through the login-protected views. |
| Personal data leaks through file names or telemetry | uuid blob names; no PII in log messages; query strings excluded from App Insights request URLs. |
| A disguised file (for example an executable renamed `.pdf`) is uploaded | Leading-byte check against the extension, on top of the extension, content-type and size checks. |
| A user misuses access to client documents | Every view, download and ZIP is recorded in `DocumentAccess` (user, action, time, IP) and reviewed monthly. |
| Accidental deletion of documents | Blob soft delete + versioning (30 days) and Postgres point-in-time restore (7 days). |
| Cost surprises | $50 budget alert, single resource group, and no pay-per-request services except the Logic App (~₹0). |

---

## 9. Open items to confirm before starting

1. **Domain:** which domain or subdomain, and who manages its DNS?
2. **GitHub:** is the repo on GitHub (needed for the Actions pipeline), or should deploys be done from your PC (zip deploy)?
3. **Email provider:** keep the current SMTP provider (port 587) or move to Azure Communication Services?
4. **Postgres version on Supabase:** check it with `select version();` and pick the same major version (or newer) on Azure.
5. **Key Vault:** use it (recommended), or plain app settings for a simpler first setup?

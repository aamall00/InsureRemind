# InsureRemind — project blueprint

Web portal for an Indian motor-insurance agent. Agents upload a client's policy copy; an LLM extracts the
policy details; the app stores them per vehicle and sends expiry reminders to the vehicle owner and the
agent by WhatsApp and email.

## Stack
- Django 5.2 (server-rendered templates + Bootstrap 5 via CDN, no SPA), Python 3.12.
- Database: Supabase Postgres (project `insureremind`, ref `xdgtgdhtkhowmpzoocll`, Mumbai `ap-south-1`) via
  `DATABASE_URL` (`dj-database-url`, `psycopg`); SQLite `db.sqlite3` when `DATABASE_URL` is empty.
  App connects as role `insureremind_app` into schema `insureremind` (role search_path), through the IPv4
  session pooler `aws-0-ap-south-1.pooler.supabase.com:5432`, user `insureremind_app.<ref>`.
  Tests use SQLite unless `TEST_DATABASE_URL` is set (must be the direct host `db.<ref>.supabase.co`,
  not the pooler — pooled idle sessions block dropping `test_postgres`).
- Config: `.env` (python-dotenv, gitignored) + `config/llm.toml` (committed, non-secret).
- LLM: OpenRouter chat-completions API called with `requests` (no Anthropic/OpenAI SDK).
  Current model `anthropic/claude-opus-5.5`, fallback `anthropic/claude-sonnet-5.5`.
- Hosting target: Azure App Service (gunicorn + WhiteNoise static files); plan and checklist in `DEPLOY_AZURE.md`.
  Documents: `STORAGE_BACKEND` `local` (`media/`) or `azure` (private Blob container via `django-storages`, managed
  identity, no SAS URLs). Telemetry: Application Insights via OpenTelemetry, only when
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is set (started in `wsgi.py`).
- Reports export: `openpyxl` (Excel) and `reportlab` (PDF, built-in Helvetica; amount columns labelled "(Rs.)").
- Email: Django `send_mail` (SMTP). WhatsApp: Meta WhatsApp Cloud API (template messages) or console backend.

## Commands (Windows, run from repo root)
```
.venv\Scripts\activate
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_demo                 # 5 demo vehicles with varied expiry dates
python manage.py runserver
python manage.py test                      # full suite; must stay green
python manage.py makemigrations --check    # must report "No changes detected"
python manage.py send_reminders [--dry-run] [--date YYYY-MM-DD]   # daily job (Task Scheduler / cron)
```

## Layout
| Path | Role |
|---|---|
| `insureremind/settings.py` | All settings; reads `.env`; `settings.LLM` from `llmconfig.py` |
| `insureremind/llmconfig.py` | Loads `config/llm.toml`; env overrides `OPENROUTER_MODEL`, `LLM_CONFIG_PATH` |
| `accounts/` | Login, `Profile` (phone), access levels (`access.py`), Manage Users pages, groups data migration `0002_access_groups` |
| `policies/models.py` | `Owner`, `Vehicle`, `Policy`, `PolicyUpload`, `DocumentAccess`; uuid blob names via `policy_document_path` / `upload_file_path` |
| `policies/audit.py` | `record_access(request, action, ...)` — writes `DocumentAccess`, never raises |
| `policies/utils.py` | `normalize_reg_no`, `normalize_phone` — the ONLY normalisers; use them everywhere |
| `policies/extraction.py` | OpenRouter request, JSON schema, prompt, `clean_extracted` (defensive parsing + warnings) |
| `policies/services.py` | `confirm_upload` — transactional save of owner/vehicle/policy + reminder scheduling |
| `policies/views.py` | Dashboard, vehicle list/detail, edit views, upload → review → confirm, auth-protected document view |
| `policies/reports.py` | `/reports/` (`report`): `filtered_policies(params)` is the ONLY filter (page + `/reports/export.xlsx` + `/reports/export.pdf`); totals; Excel/PDF builders |
| `policies/documents.py` | `document_filename` (`REGNO_policyno_startdate.ext`), `document_available`; single download = `policy_document?download=1`, all = `vehicles/<pk>/documents.zip` (`vehicle_documents_zip`) |
| `notifications/models.py` | `Reminder` |
| `notifications/scheduling.py` | `schedule_reminders`, `on_policy_created`, `reschedule`, superseding old policies |
| `notifications/backends.py` | `send_email`, `send_whatsapp` (`console` / `meta`), `send(reminder)` dispatcher |
| `notifications/sending.py` | `send_due_reminders()` — shared by the command and the cron endpoint |
| `notifications/management/commands/send_reminders.py` | Idempotent daily sender with retries |
| `insureremind/ops_views.py` | `GET /healthz`; `POST /internal/cron/send-reminders/` (`X-Cron-Token` vs `CRON_TOKEN`; 404 if unset, 403 if wrong, `?dry_run=1`, JSON counts) |
| `insureremind/telemetry.py` | `setup_telemetry()` (called from `wsgi.py`); excludes `healthz` and `/reports/` from traces (query strings hold reg nos) |
| `deploy/azure/`, `.github/workflows/azure-deploy.yml` | `startup.sh`, Logic App recurrence JSON; CI tests + deploy (deploy job runs only if repo variable `AZURE_WEBAPP_NAME` is set) |
| `templates/` | Bootstrap templates per app; `partials/` for form fields, pagination, status badge |

## Domain model
- **Vehicle** is the unique business key: `registration_no` stored normalised (uppercase, no spaces/hyphens/dots,
  e.g. `MH12AB1234`). Fields: make, model, variant, fuel, mfg year, chassis, engine, `rto_location`,
  `registration_date`, `vehicle_usage`; FK `Owner`.
- **Owner**: name (= "Name of the Insured"), phone (E.164, default +91), email, `whatsapp_opt_in`.
- **Policy**: many per vehicle (renewals keep history). insurer, policy_number, policy_type
  (comprehensive / third_party / own_damage / other), `premium` (= TOTAL incl. GST), `net_premium`, `gst_amount`,
  `ncb_percent`, start/expiry dates, document, `extracted_data` (raw LLM JSON), and previous-policy fields
  (`previous_policy_number`, `previous_insurer`, `previous_policy_type`, `previous_policy_start/end`,
  `previous_ncb_percent`, `previous_claims_count`). `is_latest` = max expiry per vehicle; only the latest gets reminders.
- **PolicyUpload**: staging row between upload and confirm (file, entered vehicle no/email/phone/consent,
  extracted data, error, status pending/confirmed/discarded).
- **Reminder**: policy, channel (email/whatsapp), recipient_type (owner/agent), recipient, days_before,
  scheduled_for, status (pending/sent/failed/skipped), attempts, last_attempt_at, error, provider_message_id.
  UniqueConstraint (policy, channel, recipient_type, days_before) is the duplicate-send guard.
- **DocumentAccess**: audit row per document view/download/zip/upload_view — user (PROTECT), action, policy/vehicle/upload
  (SET_NULL), denormalised registration_no/policy_number, ip_address, at. Read-only in admin, superuser only.

## Key flows
1. **Upload** (Read & Write users): vehicle no + client email + phone + WhatsApp consent + file
   (PDF/JPG/PNG/WEBP ≤ 10 MB) → `PolicyUpload` → LLM extraction → review page.
2. **Review**: document shown beside an editable form in three sections — Insured & Vehicle Details,
   Current Policy & Premium, Previous Policy Details. Non-blocking warnings: extracted reg no ≠ entered,
   previous end ≥ current start, expiry ≤ start, net > total. Extraction failure ⇒ banner + empty form;
   manual entry must always work.
3. **Confirm**: `confirm_upload` get-or-creates owner/vehicle (updates details), creates policy, schedules reminders,
   and skips pending reminders of the vehicle's older policies.
4. **Reminders**: `REMINDER_DAYS` (default 30,15,7,1) × channel × recipient (owner + agent from
   `AGENT_NOTIFY_EMAIL` / `AGENT_NOTIFY_PHONE`). No WhatsApp to owner without opt-in (row = skipped).
   Past dates are not scheduled. Editing a policy/owner rebuilds its unsent reminders.
   `send_reminders` sends pending rows with `scheduled_for ≤ today`, policy still latest and not expired;
   failed rows retry on later days up to `REMINDER_MAX_ATTEMPTS` (3). Running it twice a day never double-sends.

## Access control
Django Groups + model permissions, created by a data migration: **Read only** (view), **Read & Write**
(view/add/change), **Admin** (superuser; Manage Users + `/admin/`). `LoginRequiredMiddleware` protects every page.
Enforce permissions server-side in every view (`PermissionRequiredMixin`), and hide write buttons for read-only
users. All users see all clients (no per-user scoping). Media files are never served publicly — documents go
through the login-protected `policy_document` / `upload_file` views.

## LLM extraction — rules learned the hard way
- **Schema limit:** Anthropic structured outputs reject schemas with > 16 union-typed (nullable) params.
  Text/date fields are plain `"string"` with `""` meaning "not present"; only numeric fields are nullable
  (currently 7). A test enforces the ≤ 16 limit — keep it passing when adding fields.
- **Current vs previous policy:** policy documents contain a "Previous Policy Details" section. The prompt must
  keep start/expiry/NCB/insurer of the CURRENT policy separate from `previous_*` fields — a wrong expiry shifts
  every reminder by a year.
- **Premiums:** `premium_total` = "Total Premium Payable" (incl. GST); `net_premium` = pre-tax
  (e.g. "Total Package Premium(A+B)"); `gst_amount` = total tax.
- Identifiers (chassis, engine, reg no, policy no) can wrap across lines — join and strip spaces.
- Phone/email on policy copies are masked (`98******56`) — never extract contact details; they come from the
  upload form.
- PDFs go as an OpenRouter `file` part with the `file-parser` plugin (`pdf_engine` in `llm.toml`); images as
  `image_url` data URLs. The answering model is stored in `extracted_data["_model"]`.
- Before changing models, check slugs and prices at `https://openrouter.ai/api/v1/models`.

## Configuration & secrets
- Secrets only in `.env` (gitignored) or host environment: `SECRET_KEY`, `OPENROUTER_API_KEY`,
  `EMAIL_HOST_PASSWORD`, `WHATSAPP_ACCESS_TOKEN`. Never in `config/llm.toml` or code.
- Other env vars: `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `DATABASE_URL`, `EMAIL_*`, `DEFAULT_FROM_EMAIL`,
  `WHATSAPP_BACKEND` (`console` | `meta`), `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_TEMPLATE_NAME`,
  `WHATSAPP_TEMPLATE_LANG`, `WHATSAPP_API_VERSION`, `REMINDER_DAYS`, `AGENCY_NAME`, `AGENT_NOTIFY_EMAIL`,
  `AGENT_NOTIFY_PHONE`, `SECURE_SSL_REDIRECT`. Document new ones in `.env.example` and the README table.
- python-dotenv does NOT override existing environment variables: a stale `OPENROUTER_API_KEY` set in Windows
  (or inherited by the shell that starts `runserver`) silently wins over `.env`.
- WhatsApp template body variables, in order: owner name, registration no, expiry date (dd-Mon-yyyy),
  days left, insurer.

## Conventions
- Tests live in `policies/tests.py`; mock `requests.post` for OpenRouter and Meta — never call real APIs in tests.
- Never put real customer data (names, reg/chassis/policy numbers) in tests, fixtures or commits — use dummy values.
- Every model change ships with a migration; keep `makemigrations --check` clean.
- Normalise reg no / phone only via `policies/utils.py`.
- Every view that serves a document calls `record_access` (only on success; 403/404 record nothing).
- Uploads are validated by extension, size and leading bytes (`policies/forms.py`); don't trust browser content-type.
- Blob names are uuids (`policies/<hex>.ext`); old files keep their names. User-facing names come from `document_filename`, never the stored path.
- Tests must not call Azure; mock the credential. Cron/health endpoints are `login_not_required`.
- Extraction failures must degrade to manual entry, never a 500.
- Dates shown as `dd-Mon-yyyy`; amounts in ₹.

## Known gaps / next steps
- Amounts render as `₹ 16077.00` (no Indian digit grouping); review page document panel is not sticky.
- OD/TP split premiums and separate OD/TP expiry dates (bundled new-car policies) not yet modelled.
- Agent details printed on the policy (agency code/name/phone) not captured; agent reminders use the global
  `AGENT_NOTIFY_*` settings.
- Live Meta WhatsApp sending untested. Full suite verified on Supabase Postgres via `TEST_DATABASE_URL`.
- Azure Phase 1 code is on `main`; Phase 2 infra is built and the app is deployed (see **Azure status** below).
- `DocumentAccess.ip_address` comes from the first `X-Forwarded-For` value: informational only, client-spoofable.
- `setup_telemetry()` lets an invalid `APPLICATIONINSIGHTS_CONNECTION_STRING` crash every gunicorn worker
  (happened on the first deploy). Planned fix: catch, log, start without telemetry, plus a test.
- In tests, never call `.close()` on a streaming/File response — it fires `request_finished` and closes the
  DB connection (invisible on SQLite in-memory, fatal on Postgres). Consume `streaming_content` instead.

## Azure status (as of 2026-11-11)
Full checklist: `DEPLOY_AZURE.md`. Phase 2 (infra) done; Phase 3 (first deploy) mostly done; custom domain done;
Phase 4 (data migration, cutover) not started. Old Supabase setup is still the live system for real clients.

**Live:** `https://app.pactbridge.in` (custom domain, free App Service managed certificate, SNI) and the long host
`https://insureremind-pactbridge-g5bdcgegg2a9gahk.centralindia-01.azurewebsites.net` (healthz 200, login,
admin, static, cron dry-run 200 / wrong token 403 all verified). `ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS` list both
hosts. The Logic App deliberately keeps calling the long host (it never changes and does not depend on DNS). Superuser `admin` exists on the Azure DB; the
password is held by the owner, never in the repo. Azure DB holds only empty tables, no client data.

| Resource | Name | Region / notes |
|---|---|---|
| Resource group | `rg-insureremind-prod` | metadata in West India (irrelevant) |
| App Service plan / Web app | `asp-insureremind-prod` (B1 Linux) / `insureremind-pactbridge` | Central India; Python 3.12, startup `bash deploy/azure/startup.sh`, system identity, health check `/healthz` |
| Postgres Flexible | `psql-insureremind-pactbridge` | **India South Central** (PG 17, B1ms); db/role/schema `insureremind` / `insureremind_app` |
| Storage | `stinsureremindpactbridge`, container `documents` | **West India**; app identity = Storage Blob Data Contributor |
| Key Vault | `kv-ir-pactbridge` (RBAC) | 6 secrets: SECRET-KEY, DATABASE-URL, OPENROUTER-API-KEY, EMAIL-HOST-PASSWORD, WHATSAPP-ACCESS-TOKEN, CRON-TOKEN; app identity = Secrets User |
| Logic App | `logic-insureremind-reminders` | in RG `logic-insureremind-reminders_group` (portal-created); daily 09:00 IST, **Disabled** until go-live; alert `Reminder run failed` → action group `ag-insureremind` |
| GitHub deploy | Entra app `gh-insureremind-deploy` | OIDC federated cred for `aamall00/InsureRemind` branch `main`; Website Contributor on the web app; repo variable `AZURE_WEBAPP_NAME` set, so **every push to `main` deploys** |
| Brand website | Static Web App `swa-pactbridge-site` (Free) | East Asia (global CDN); serves `https://www.pactbridge.in` from repo `aamall00/pactbridge-site` (`site/`), deployed by GitHub Actions with secret `AZURE_STATIC_WEB_APPS_API_TOKEN` |
| DNS | GoDaddy (registrar + DNS) | CNAME `app` → web app host, TXT `asuid.app` → verification id, CNAME `www` → SWA host; apex `pactbridge.in` forwarded (301) to `https://www.pactbridge.in` |

**Domains:** PactBridge is the umbrella brand. `www.pactbridge.in` = brand landing page (edit `pactbridge-site/site/index.html`;
`landing/index.html` in this repo is only a copy of the source and does not deploy). `www.pactbridge.in/insureremind` forwards
to the page's `#insureremind` section. The app always stays on the `app` subdomain, never under a path.

**Lessons learned (apply when touching Azure):**
- The web app was created with *secure unique default hostname* ON: the short `insureremind-pactbridge.azurewebsites.net`
  does not exist and this cannot be changed later. `ALLOWED_HOSTS`/`CSRF_TRUSTED_ORIGINS`/Logic App URI must use the real
  host (or the custom domain). A wrong host shows up as HTTP 400 (DisallowedHost), not a DNS error.
- The Oryx build unpacks the app into `/tmp/<id>/` (venv `antenv`), not `/home/site/wwwroot`. In SSH:
  `cd "$(dirname "$(ls -t /tmp/*/manage.py | head -1)")" && source antenv/bin/activate`. `az webapp ssh` is
  interactive only; for one-off management commands, run them from the PC against Azure Postgres with a temporary
  firewall rule for the PC's IP (the owner's ISP IP is dynamic) and delete the rule afterwards.
- New az CLI: `az postgres flexible-server firewall-rule create --server-name <pg> --name <rule>` (old `--name`/`--rule-name` fails).
- Postgres firewall: one rule per App Service outbound IPv4 (`app-1`..`app-31`); IPv6 is not supported. If the plan
  or tier changes, the outbound IPs can change — re-sync the rules.
- Metric alerts check at most every 1 hour (24 h lookback is fine for a daily run).
- Azure CLI is installed on the owner's PC at `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd` (not on Git
  Bash PATH; call it from PowerShell) and logged in as the subscription owner (personal Microsoft account).
- The GitHub repo is **public**: never commit secrets, IPs of the owner, or client data; consider making it private.
- `az webapp config ssl create` for a managed certificate returns before the cert exists (and prints a harmless
  deserialization traceback); `az webapp config ssl list` showed `[]` even when it existed. Get the thumbprint with
  `az resource show --resource-type Microsoft.Web/certificates -n <hostname>`, then `ssl bind --ssl-type SNI`. After binding,
  TLS is flaky for a few minutes (`SEC_E_WRONG_PRINCIPAL` on some front ends) until it propagates.
- Static Web Apps: a `staticwebapp.config.json` redirect to a target containing `#` made the deploy step fail; use a small
  forwarding page instead.
- GoDaddy domain forwarding only redirects the bare apex (`pactbridge.in/` → www); it drops paths (`pactbridge.in/x` = 404).
- The Logic App definition holds `X-Cron-Token` in plain text; anyone reading the workflow definition sees it.

**Pending (in order):**
1. **Move DNS to Azure DNS** (about $0.50/month) so the apex `pactbridge.in` points straight at the Static Web App and paths
   like `pactbridge.in/insureremind` work. First ask whether email on `pactbridge.in` is used (MX/SPF/DKIM must be copied)
   and get the full GoDaddy record list. Create zone `pactbridge.in` in `rg-insureremind-prod`; recreate `app`, `asuid.app`,
   `www`, plus an apex alias record → `swa-pactbridge-site` and add the apex hostname on the SWA; the owner switches the
   GoDaddy nameservers to the 4 Azure NS; remove the GoDaddy forward; check that both certificates still renew.
2. **Rotate `CRON_TOKEN`** (its value was exposed in a chat transcript on 2026-11-11): new value in Key Vault `CRON-TOKEN`
   and in the Logic App header; verify cron dry-run 200 with the new token and 403 with the old one.
3. **Landing page placeholders:** contact phone/WhatsApp, email, office and the IRDAI registration number still read
   "to be added" on `www.pactbridge.in`.
4. **Region mismatch:** Postgres (India South Central) and storage (West India) are not in the app's region (Central India).
   Both are still empty; recreate in Central India before the data migration if the owner agrees (decision pending).
5. **Application Insights not created:** create `appi-insureremind` + workspace `log-insureremind` (30-day retention)
   in Central India, then set `APPLICATIONINSIGHTS_CONNECTION_STRING` (currently removed). Fix `setup_telemetry()` first.
6. **Phase 3 smoke test:** upload a dummy PDF (tests the OPENROUTER Key Vault reference and blob storage), review,
   confirm, view/download/ZIP, check DocumentAccess rows and uuid blob names; delete the dummy data afterwards.
7. Set the real `AGENCY_NAME`, `AGENT_NOTIFY_EMAIL`, `AGENT_NOTIFY_PHONE`; email/WhatsApp are still `console` backends.
8. Optional tidy-ups: move the Logic App, action group and alert into `rg-insureremind-prod` (re-check the alert scope);
   delete the stale Postgres rule `ClientIPAddress_2026-11-9_*`.
9. Phase 4: data migration Supabase → Azure (pg_dump 17+), document copy to blob, enable the Logic App, cutover.

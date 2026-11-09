# InsureRemind

An insurance-agent portal (Django 5, Bootstrap 5, server-rendered) that stores motor insurance policies and sends
expiry reminders to clients and the agent by **WhatsApp** and **email**. Policy PDFs/photos are read with the
OpenRouter API; the agent reviews and confirms every extraction before anything is saved.

## Features

- Login-only portal; three access levels (Read only, Read & Write, Admin) built on Django Groups + model permissions.
- In-portal **Manage Users** (admins): create, change access level, activate/deactivate, reset password. `/admin/` is enabled too.
- Owners, vehicles (unique, normalised registration number) and policies with renewal history; only the latest policy per vehicle gets reminders.
- Two-step upload: upload document, AI extraction, side-by-side review/edit, confirm. Manual entry always works.
- Reminder schedule (`REMINDER_DAYS` before expiry) per channel and recipient, sent by a daily management command.
- Documents are served only through authenticated views; `MEDIA_ROOT` is never exposed.

## Setup (Windows PowerShell)

```powershell
cd InsureRemind
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # then edit .env
python manage.py migrate      # also creates the 3 access-level groups
python manage.py createsuperuser   # first admin (an Admin-level user)
python manage.py seed_demo    # optional demo data
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in. Further users are created by an admin under **Manage Users**.
Run the tests with `python manage.py test`.

Linux/macOS: `python3 -m venv .venv && source .venv/bin/activate`, the rest is identical.

## Access levels

| Level | Permissions |
|-------|-------------|
| Read only | view owner / vehicle / policy / upload / reminder |
| Read & Write | view + add + change on the same models (upload policies, edit records) |
| Admin | `is_staff` + `is_superuser`: everything, including Manage Users and `/admin/` |

Groups and permissions are created by the data migration `accounts/migrations/0002_access_groups.py`.
Superusers made with `createsuperuser` show as Admin. Every user sees all clients.

## Configuration (environment / `.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DEBUG` | `false` | `true` for development. When false, secure cookies, HSTS and SSL redirect are enabled. |
| `SECRET_KEY` | (required if `DEBUG=false`) | Django secret key. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` in debug | Comma-separated hosts. |
| `CSRF_TRUSTED_ORIGINS` | empty | e.g. `https://portal.example.com`. |
| `SECURE_SSL_REDIRECT` | `true` (when not debug) | Set `false` if TLS is terminated upstream without `X-Forwarded-Proto`. |
| `DATABASE_URL` | SQLite `db.sqlite3` | Postgres URL, e.g. Supabase session pooler — see "Database: Supabase". |
| `TEST_DATABASE_URL` | empty (tests use SQLite) | Optional Postgres URL for `manage.py test`; use a direct (non-pooled) host. |
| `STORAGE_BACKEND` | `local` | `local` (`media/` folder) or `azure` (private Blob container). |
| `AZURE_STORAGE_ACCOUNT_NAME` | empty | Storage account name (azure backend; managed identity auth). |
| `AZURE_STORAGE_CONTAINER` | `documents` | Blob container name (azure backend). |
| `AZURE_STORAGE_CONNECTION_STRING` | empty | Local testing only; overrides managed identity. Never in production. |
| `CRON_TOKEN` | empty | Secret for `POST /internal/cron/send-reminders/` (`X-Cron-Token` header). Empty disables the endpoint (404). |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | empty | Enables Azure Application Insights telemetry (empty = off). |
| `OPENROUTER_API_KEY` | empty | Secret key for AI extraction (https://openrouter.ai/keys). If empty, the review page shows a banner and manual entry. |
| `OPENROUTER_MODEL` | from `config/llm.toml` | Optional override of the model slug. |
| `LLM_CONFIG_PATH` | `config/llm.toml` | Optional path to another LLM settings file. |
| `AGENCY_NAME` | `Your Insurance Agency` | Shown in reminder messages. |
| `REMINDER_DAYS` | `30,15,7,1` | Days before expiry to remind. |
| `AGENT_NOTIFY_EMAIL` | empty | Agent also receives every reminder here (skipped if empty). |
| `AGENT_NOTIFY_PHONE` | empty | Agent WhatsApp number (E.164 or Indian 10-digit). |
| `EMAIL_BACKEND` | console backend | Use `django.core.mail.backends.smtp.EmailBackend` for real email. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | | SMTP settings. |
| `DEFAULT_FROM_EMAIL` | `reminders@example.com` | From address. |
| `WHATSAPP_BACKEND` | `console` | `console` (log only, marks sent) or `meta` (WhatsApp Cloud API). |
| `WHATSAPP_API_VERSION` | `v21.0` | Graph API version. |
| `WHATSAPP_PHONE_NUMBER_ID` | | From the Meta app dashboard. |
| `WHATSAPP_ACCESS_TOKEN` | | Permanent system-user token. |
| `WHATSAPP_TEMPLATE_NAME` | `policy_expiry_reminder` | Approved template name. |
| `WHATSAPP_TEMPLATE_LANG` | `en` | Template language code. |

## Database: Supabase (Postgres)

Production data lives in a Supabase project (region `ap-south-1`, Mumbai). The app connects as its own
login role into a private schema, not as `postgres` and not into `public`.

1. **Create the project** at https://supabase.com/dashboard → New project → region *South Asia (Mumbai)*.
2. **Create the app role and schema** (Dashboard → SQL Editor). Use a long random password:
   ```sql
   create role insureremind_app with login password '<strong-password>';
   grant insureremind_app to postgres;              -- needed on Postgres 16+ to hand over the schema
   create schema insureremind authorization insureremind_app;
   alter role insureremind_app set search_path = insureremind, public;
   alter role insureremind_app createdb;            -- optional: only to run tests on Postgres
   ```
   The `insureremind` schema is not exposed by Supabase's Data API, and `anon` / `authenticated`
   have no privileges on it, so client data is only reachable through this Django app.
3. **Connection string** — use the **session pooler** (IPv4, works from any network). Find it under
   Dashboard → Connect → Session pooler. Note the username is `<role>.<project-ref>`:
   ```
   DATABASE_URL=postgres://insureremind_app.<project-ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require
   ```
   Put it in `.env` (local) or the host's secret store (production). Never commit it.
4. **Create the tables:** `python manage.py migrate`, then `python manage.py createsuperuser`.
5. **Moving existing SQLite data** (optional):
   ```
   $env:DATABASE_URL="sqlite:///<full path>/db.sqlite3"
   python manage.py dumpdata auth.user accounts policies notifications --natural-foreign -o data.json
   Remove-Item Env:DATABASE_URL
   python manage.py loaddata data.json
   ```
6. **Tests** use SQLite by default. To run them on Postgres set `TEST_DATABASE_URL` to the **direct** host
   (`db.<project-ref>.supabase.co`, user `insureremind_app`) — the pooler keeps idle sessions that stop Django
   dropping the test database. The direct host is IPv6-only on Supabase's free plan.

Notes:
- Uploaded policy documents are not stored in Supabase: they go to `media/` (`STORAGE_BACKEND=local`) or Azure Blob Storage (`STORAGE_BACKEND=azure`).
- Supabase free projects pause after a period of inactivity; resume them from the dashboard.
- Back-ups: daily on paid plans; on the free plan export with `pg_dump` via the session pooler.

## AI extraction (OpenRouter)

Policy documents are read through [OpenRouter](https://openrouter.ai) (OpenAI-compatible chat completions API, plain `requests`).

1. Create a key at https://openrouter.ai/keys and put it in `.env` as `OPENROUTER_API_KEY=...` (never in a committed file).
2. Non-secret settings live in the committed file `config/llm.toml`: `model`, `fallback_models`, `pdf_engine`
   (`native`, `mistral-ocr` or `pdf-text`), timeout, `max_tokens`, `temperature`, and optional `app_title` / `app_url`
   attribution headers. Change the model there (check slugs at https://openrouter.ai/models) or set `OPENROUTER_MODEL`.
3. **In production** set `OPENROUTER_API_KEY` as a real environment variable or in the host's secret store, not in a committed file.

The extracted fields include insured/vehicle details, current premium breakdown (net, GST, total), NCB and the previous
policy; the agent reviews and edits everything before saving. Contact details are never read from the document.

## Reports

**Reports** in the navbar (`/reports/`, any user with view access, including Read only) searches policies by owner name
(contains), vehicle number (normalised, partial match such as `mh12`) and expiry month, optionally showing only the latest
policy per vehicle (default). The result table shows totals for net premium, GST and total premium across all matching rows.
**Export Excel** (`openpyxl`) and **Export PDF** (`reportlab`, landscape A4; amount columns are labelled "(Rs.)") download every
matching row using the same filters.

Policy documents can be downloaded from the vehicle page (one by one, or "Download all policies (ZIP)").

## Reminders

Creating a policy schedules one `Reminder` row for each of `REMINDER_DAYS` x {email, WhatsApp} x {owner, agent}.
Past dates are not created; WhatsApp to an owner who has not opted in is stored as `skipped`. When a newer policy is
added for the same vehicle, the old policy's pending reminders are marked `skipped`. A unique constraint on
(policy, channel, recipient type, days before) guards against duplicates.

```
python manage.py send_reminders [--dry-run] [--date YYYY-MM-DD]
```

Sends every pending reminder with `scheduled_for <= today` whose policy is still the latest and not expired. Results
are `sent` or `failed`; failed reminders are retried on later days (not the same day) up to 3 attempts. Running it twice in a
day never double-sends (rows are locked with `select_for_update(skip_locked=True)` where the database supports it
and the status is re-checked inside the transaction).

### Schedule it daily

**Windows Task Scheduler** (09:00 daily):

```powershell
schtasks /Create /SC DAILY /ST 09:00 /TN "InsureRemind reminders" /TR "cmd /c cd /d C:\path\to\InsureRemind && .venv\Scripts\python.exe manage.py send_reminders >> reminders.log 2>&1"
```

**Linux cron** (`crontab -e`):

```
0 9 * * * cd /srv/insureremind && .venv/bin/python manage.py send_reminders >> /var/log/insureremind-reminders.log 2>&1
```

**Hosted alternative:** set `CRON_TOKEN` and have a scheduler (for example an Azure Logic App) call
`POST /internal/cron/send-reminders/` with header `X-Cron-Token: <token>` daily. It runs the same code as the command and
returns JSON counts; add `?dry_run=1` to test. It returns 404 when `CRON_TOKEN` is empty and 403 for a wrong token.
`GET /healthz` (no login) returns `ok` after a database check, for health probes.

## WhatsApp setup (Meta Cloud API)

1. Create a Meta Business account (business.facebook.com) and verify the business.
2. At developers.facebook.com create an app of type **Business** and add the **WhatsApp** product.
3. Add / register the sending phone number; copy its **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`.
4. In Business Settings create a **System User**, grant it the WhatsApp app with `whatsapp_business_messaging` and
   `whatsapp_business_management`, and generate a **permanent token** into `WHATSAPP_ACCESS_TOKEN`.
5. In WhatsApp Manager create a message template (below) and wait for approval. Put its name in `WHATSAPP_TEMPLATE_NAME`
   and language in `WHATSAPP_TEMPLATE_LANG`.
6. Set `WHATSAPP_BACKEND=meta`. Test with `seed_demo` and `send_reminders` using your own number first.

### Suggested template (category: Utility, language: English, 5 body variables)

```
Hello {{1}}, your motor insurance policy for vehicle {{2}} expires on {{3}} ({{4}} days left). Insurer: {{5}}.
Please contact us to renew on time and avoid a lapse in cover.
```

Variables in order: 1 owner name, 2 registration number, 3 expiry date (`dd-Mon-yyyy`), 4 days left, 5 insurer.
The same template is used for owner and agent messages. Clients must have given consent (the upload form checkbox).

## Deployment notes

- Set `DEBUG=false`, a strong `SECRET_KEY`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` and a Postgres `DATABASE_URL`
  (`psycopg` is in `requirements.txt`).
- Run behind a TLS-terminating proxy that sets `X-Forwarded-Proto`; use gunicorn (Linux) or waitress (Windows).
- Static files: only Django admin uses them; run `collectstatic` and serve `STATIC_ROOT` (e.g. via the proxy or WhiteNoise).
- Back up the database and the documents (`MEDIA_ROOT` for the local backend; Blob soft delete/versioning for Azure). Keep `MEDIA_ROOT` outside any web-served path.
- Schedule `send_reminders` daily (above). `python manage.py check --deploy` is clean apart from `security.W019`
  (`X_FRAME_OPTIONS=SAMEORIGIN` is intentional so policy PDFs can be embedded in the review page).
- Uploaded documents are sent to OpenRouter (and the underlying model provider) for extraction; mention this in your privacy notice.

## Deploying to Azure

The app runs on Azure App Service (gunicorn + WhiteNoise) with PostgreSQL Flexible Server, private Blob Storage
(`STORAGE_BACKEND=azure`, managed identity) and Application Insights. Startup script, Logic App definition and GitHub
Actions workflow are in `deploy/azure/` and `.github/workflows/`. Document views, downloads and ZIPs are recorded in
`DocumentAccess` (read-only in `/admin/`). See [DEPLOY_AZURE.md](DEPLOY_AZURE.md) for the full plan and checklist.

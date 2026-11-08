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
| `DATABASE_URL` | SQLite `db.sqlite3` | e.g. `postgres://user:pass@host:5432/insureremind`. |
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

## AI extraction (OpenRouter)

Policy documents are read through [OpenRouter](https://openrouter.ai) (OpenAI-compatible chat completions API, plain `requests`).

1. Create a key at https://openrouter.ai/keys and put it in `.env` as `OPENROUTER_API_KEY=...` (never in a committed file).
2. Non-secret settings live in the committed file `config/llm.toml`: `model`, `fallback_models`, `pdf_engine`
   (`native`, `mistral-ocr` or `pdf-text`), timeout, `max_tokens`, `temperature`, and optional `app_title` / `app_url`
   attribution headers. Change the model there (check slugs at https://openrouter.ai/models) or set `OPENROUTER_MODEL`.
3. **In production** set `OPENROUTER_API_KEY` as a real environment variable or in the host's secret store, not in a committed file.

The extracted fields include insured/vehicle details, current premium breakdown (net, GST, total), NCB and the previous
policy; the agent reviews and edits everything before saving. Contact details are never read from the document.

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
- Back up the database and `MEDIA_ROOT` (uploaded policy documents). Keep `MEDIA_ROOT` outside any web-served path.
- Schedule `send_reminders` daily (above). `python manage.py check --deploy` is clean apart from `security.W019`
  (`X_FRAME_OPTIONS=SAMEORIGIN` is intentional so policy PDFs can be embedded in the review page).
- Uploaded documents are sent to OpenRouter (and the underlying model provider) for extraction; mention this in your privacy notice.

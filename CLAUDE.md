# InsureRemind — project blueprint

Web portal for an Indian motor-insurance agent. Agents upload a client's policy copy; an LLM extracts the
policy details; the app stores them per vehicle and sends expiry reminders to the vehicle owner and the
agent by WhatsApp and email.

## Stack
- Django 5.2 (server-rendered templates + Bootstrap 5 via CDN, no SPA), Python 3.12.
- SQLite in dev; Postgres in prod via `DATABASE_URL` (`dj-database-url`, `psycopg`).
- Config: `.env` (python-dotenv, gitignored) + `config/llm.toml` (committed, non-secret).
- LLM: OpenRouter chat-completions API called with `requests` (no Anthropic/OpenAI SDK).
  Current model `anthropic/claude-opus-5.5`, fallback `anthropic/claude-sonnet-5.5`.
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
| `policies/models.py` | `Owner`, `Vehicle`, `Policy`, `PolicyUpload` |
| `policies/utils.py` | `normalize_reg_no`, `normalize_phone` — the ONLY normalisers; use them everywhere |
| `policies/extraction.py` | OpenRouter request, JSON schema, prompt, `clean_extracted` (defensive parsing + warnings) |
| `policies/services.py` | `confirm_upload` — transactional save of owner/vehicle/policy + reminder scheduling |
| `policies/views.py` | Dashboard, vehicle list/detail, edit views, upload → review → confirm, auth-protected document view |
| `policies/reports.py` | `/reports/` (`report`): `filtered_policies(params)` is the ONLY filter (page + `/reports/export.xlsx` + `/reports/export.pdf`); totals; Excel/PDF builders |
| `policies/documents.py` | `document_filename` (`REGNO_policyno_startdate.ext`), `document_available`; single download = `policy_document?download=1`, all = `vehicles/<pk>/documents.zip` (`vehicle_documents_zip`) |
| `notifications/models.py` | `Reminder` |
| `notifications/scheduling.py` | `schedule_reminders`, `on_policy_created`, `reschedule`, superseding old policies |
| `notifications/backends.py` | `send_email`, `send_whatsapp` (`console` / `meta`), `send(reminder)` dispatcher |
| `notifications/management/commands/send_reminders.py` | Idempotent daily sender with retries |
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
- Extraction failures must degrade to manual entry, never a 500.
- Dates shown as `dd-Mon-yyyy`; amounts in ₹.

## Known gaps / next steps
- Amounts render as `₹ 16077.00` (no Indian digit grouping); review page document panel is not sticky.
- OD/TP split premiums and separate OD/TP expiry dates (bundled new-car policies) not yet modelled.
- Agent details printed on the policy (agency code/name/phone) not captured; agent reminders use the global
  `AGENT_NOTIFY_*` settings.
- Live Meta WhatsApp sending untested; Postgres untested.

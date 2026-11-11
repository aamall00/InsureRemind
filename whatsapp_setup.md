# WhatsApp setup (Meta Cloud API)

Status (2026-11-11): Meta developer app registered. Nothing below done yet. The app code is ready:
`WHATSAPP_BACKEND=meta` sends an approved template message via the Cloud API (`notifications/backends.py`).
Live sending has never been tested.

Do it in two stages: test with Meta's free test number first, then switch to the real business number.

## Stage 1 — Test with Meta's test number (~30 min)

- [ ] **Open the WhatsApp product.** developers.facebook.com → My Apps → your app → **WhatsApp → API Setup**.
      If WhatsApp isn't listed: "Add product" → WhatsApp, and pick/create a Meta Business account.
- [ ] **Note two IDs** on the API Setup page:
  - **Phone number ID** (under the "From" test number) → `WHATSAPP_PHONE_NUMBER_ID`. Not a secret.
  - **WhatsApp Business Account ID** (only needed for templates).
- [ ] **Add your own mobile as a recipient.** "To" → Manage phone number list → add and verify with the code
      WhatsApp sends. The test number can only message up to 5 verified numbers.
- [ ] **Press "Send message"** on API Setup. You should receive Meta's `hello_world` message — proves the account works.
- [ ] **Create the template.** business.facebook.com → WhatsApp Manager → Message templates → Create template:
  - Category: **Utility**. Name: **`policy_expiry_reminder`**.
  - Language: **English** (code `en`). If you choose "English (US)" the code is `en_US` — set
    `WHATSAPP_TEMPLATE_LANG` to match.
  - Body:
    ```
    Hello {{1}}, your motor insurance policy for vehicle {{2}} expires on {{3}} ({{4}} days left). Insurer: {{5}}.
    Please contact us to renew on time and avoid a lapse in cover.
    ```
  - Sample values (dummy): `Ravi Kumar`, `MH12AB1234`, `15-Dec-2026`, `30`, `Sample General Insurance`.
  - Submit. Utility templates are usually approved within minutes to a few hours.
- [ ] **Copy the temporary access token** from API Setup (expires in ~24 h — fine for testing).

## Stage 2 — Production (before real clients get messages)

- [ ] **Business verification:** Business Settings → Security Center → Start verification (GST certificate, PAN
      or similar). Without it you're limited to a small number of new conversations per day.
- [ ] **Real sending number:** API Setup → "Add phone number". Use a number **not** currently active on the normal
      WhatsApp or WhatsApp Business app (a spare SIM is ideal; it only needs to receive one verification SMS/call).
      Display name = agency name (Meta reviews it).
- [ ] **Permanent token:** Business Settings → Users → **System users** → Add (role Admin) → Assign assets: the app
      (full control) and the WhatsApp account → **Generate token** with `whatsapp_business_messaging` and
      `whatsapp_business_management`, expiry **Never**. Store it only in `.env` / Key Vault — never in the repo or chat.
- [ ] **Payment method:** WhatsApp Manager → Payment settings. Meta charges per utility message to Indian numbers
      (a fraction of a rupee each; check Meta's current pricing page).
- [ ] Recreate / get approval for the same template under the production WhatsApp account if it differs from the test one.

## Stage 3 — Configure the app

Local `.env`:
```
WHATSAPP_BACKEND=meta
WHATSAPP_PHONE_NUMBER_ID=<phone number id>
WHATSAPP_ACCESS_TOKEN=<token>
WHATSAPP_TEMPLATE_NAME=policy_expiry_reminder
WHATSAPP_TEMPLATE_LANG=en
WHATSAPP_API_VERSION=<version in the API Setup sample curl, e.g. v2x.0>
```
The code default `v21.0` is old and may be deprecated — use the version Meta shows in the sample curl.

Azure (after production works):
- Update Key Vault secret `WHATSAPP-ACCESS-TOKEN` in `kv-ir-pactbridge` with the permanent token.
- Set app settings on `insureremind-pactbridge`: `WHATSAPP_BACKEND=meta`, `WHATSAPP_PHONE_NUMBER_ID`,
  `WHATSAPP_TEMPLATE_NAME`, `WHATSAPP_TEMPLATE_LANG`, `WHATSAPP_API_VERSION`.
- Also set real `AGENT_NOTIFY_PHONE` (agent's WhatsApp number) and `AGENCY_NAME`.

## Stage 4 — Test (with Claude)

Tell Claude the Phone number ID and the template language code once Stage 1 is done. Then:
- [ ] Create a dummy policy locally with your own number as the owner phone (WhatsApp consent ticked) and expiry 7 days out.
- [ ] `python manage.py send_reminders --dry-run`, then a real run.
- [ ] Confirm the message arrives and the `Reminder` row is `sent` with Meta's message id in `provider_message_id`.
- [ ] Delete the dummy data.

## Open decision

The agent's copy uses the same template, so the agent receives "Hello *client name*, your policy…".
Option: a second Utility template for the agent, e.g.
`Client {{1}}'s motor policy for vehicle {{2}} expires on {{3}} ({{4}} days left). Insurer: {{5}}.`
— needs a small code change (`WHATSAPP_AGENT_TEMPLATE_NAME`). Decide before creating templates.

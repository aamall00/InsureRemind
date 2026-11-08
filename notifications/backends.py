"""Notification backends. Each send_* returns a provider message id (str, may be empty) or raises."""
import logging

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from django.utils.html import escape

logger = logging.getLogger(__name__)


class SendError(Exception):
    pass


def message_context(reminder):
    policy = reminder.policy
    vehicle = policy.vehicle
    owner = vehicle.owner
    days_left = (policy.expiry_date - timezone.localdate()).days
    return {
        "owner_name": owner.name,
        "registration_no": vehicle.registration_no,
        "expiry_date": policy.expiry_date.strftime("%d-%b-%Y"),
        "days_left": days_left,
        "insurer": policy.insurer,
        "policy_number": policy.policy_number,
        "agency": settings.AGENCY_NAME,
        "for_agent": reminder.recipient_type == "agent",
    }


def _days_text(n):
    if n <= 0:
        return "today"
    return f"in {n} day{'s' if n != 1 else ''}"


def build_email(reminder):
    c = message_context(reminder)
    subject = f"Insurance renewal due {_days_text(c['days_left'])} – {c['registration_no']}"
    if c["for_agent"]:
        greeting = f"Client {c['owner_name']}'s policy is due for renewal."
    else:
        greeting = f"Dear {c['owner_name']}, your motor insurance policy is due for renewal."
    lines = [
        greeting,
        f"Vehicle: {c['registration_no']}",
        f"Insurer: {c['insurer']} (policy {c['policy_number']})",
        f"Expiry date: {c['expiry_date']} ({_days_text(c['days_left'])})",
        "",
        f"Please contact {c['agency']} to renew on time and avoid a lapse in cover.",
    ]
    text = "\n".join(lines)
    html = "<p>{}</p><ul><li>Vehicle: <b>{}</b></li><li>Insurer: {} (policy {})</li>" \
           "<li>Expiry date: <b>{}</b> ({})</li></ul><p>Please contact {} to renew on time " \
           "and avoid a lapse in cover.</p>".format(
               escape(greeting), escape(c["registration_no"]), escape(c["insurer"]), escape(c["policy_number"]),
               escape(c["expiry_date"]), escape(_days_text(c["days_left"])), escape(c["agency"]))
    return subject, text, html


def send_email(reminder):
    if not reminder.recipient:
        raise SendError("No email address")
    subject, text, html = build_email(reminder)
    send_mail(subject, text, settings.DEFAULT_FROM_EMAIL, [reminder.recipient],
              html_message=html, fail_silently=False)
    return ""


def whatsapp_params(reminder):
    c = message_context(reminder)
    return [c["owner_name"], c["registration_no"], c["expiry_date"], str(c["days_left"]), c["insurer"]]


def whatsapp_payload(reminder):
    return {
        "messaging_product": "whatsapp",
        "to": reminder.recipient.lstrip("+"),
        "type": "template",
        "template": {
            "name": settings.WHATSAPP_TEMPLATE_NAME,
            "language": {"code": settings.WHATSAPP_TEMPLATE_LANG},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": v} for v in whatsapp_params(reminder)]}
            ],
        },
    }


def send_whatsapp_console(reminder):
    p = whatsapp_params(reminder)
    logger.info("[WhatsApp console] to=%s: %s's policy for %s expires %s (%s days left), insurer %s",
                reminder.recipient, *p)
    return "console"


def send_whatsapp_meta(reminder):
    if not (settings.WHATSAPP_PHONE_NUMBER_ID and settings.WHATSAPP_ACCESS_TOKEN):
        raise SendError("WhatsApp Cloud API is not configured (WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN)")
    url = (f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/"
           f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages")
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"},
            json=whatsapp_payload(reminder),
            timeout=15,
        )
    except requests.RequestException as e:
        raise SendError(f"WhatsApp request failed: {e}")
    if not 200 <= resp.status_code < 300:
        raise SendError(f"WhatsApp API {resp.status_code}: {resp.text[:1000]}")
    try:
        return resp.json()["messages"][0]["id"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise SendError(f"WhatsApp API returned unexpected body: {resp.text[:500]}")


def send_whatsapp(reminder):
    if settings.WHATSAPP_BACKEND == "meta":
        return send_whatsapp_meta(reminder)
    return send_whatsapp_console(reminder)


def send(reminder):
    if reminder.channel == "email":
        return send_email(reminder)
    return send_whatsapp(reminder)

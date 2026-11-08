"""Creation of the Reminder schedule for a policy."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import Reminder


def skip_pending_for_other_policies(policy):
    """A newer policy supersedes the old ones: their pending reminders are skipped."""
    return Reminder.objects.filter(
        policy__vehicle=policy.vehicle, status=Reminder.PENDING
    ).exclude(policy=policy).update(status=Reminder.SKIPPED, error="Superseded by a newer policy")


def _recipients(policy):
    """Yield (channel, recipient_type, recipient, skip_reason)."""
    owner = policy.vehicle.owner
    if owner.email:
        yield Reminder.EMAIL, Reminder.OWNER, owner.email, ""
    if owner.phone:
        yield (Reminder.WHATSAPP, Reminder.OWNER, owner.phone,
               "" if owner.whatsapp_opt_in else "Owner has not opted in to WhatsApp")
    if settings.AGENT_NOTIFY_EMAIL:
        yield Reminder.EMAIL, Reminder.AGENT, settings.AGENT_NOTIFY_EMAIL, ""
    if settings.AGENT_NOTIFY_PHONE:
        yield Reminder.WHATSAPP, Reminder.AGENT, settings.AGENT_NOTIFY_PHONE, ""


def schedule_reminders(policy, today=None):
    """Create reminder rows for REMINDER_DAYS x channel x recipient. Idempotent."""
    today = today or timezone.localdate()
    created = []
    for days in settings.REMINDER_DAYS:
        when = policy.expiry_date - timedelta(days=days)
        if when < today:
            continue
        for channel, rtype, recipient, skip_reason in _recipients(policy):
            rem, was_created = Reminder.objects.get_or_create(
                policy=policy, channel=channel, recipient_type=rtype, days_before=days,
                defaults={
                    "recipient": recipient,
                    "scheduled_for": when,
                    "status": Reminder.SKIPPED if skip_reason else Reminder.PENDING,
                    "error": skip_reason,
                },
            )
            if was_created:
                created.append(rem)
    return created


def on_policy_created(policy):
    """Call after a policy is saved. Only the latest policy per vehicle gets reminders."""
    if not policy.is_latest:
        return []
    skip_pending_for_other_policies(policy)
    return schedule_reminders(policy)


def reschedule(policy):
    """After editing a policy (e.g. new expiry date) rebuild its not-yet-sent reminders."""
    Reminder.objects.filter(policy=policy, status__in=[Reminder.PENDING, Reminder.SKIPPED]).delete()
    return on_policy_created(policy)

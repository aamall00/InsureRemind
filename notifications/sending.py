from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from notifications import backends
from notifications.models import Reminder


def _process(pk, today, stderr=None):
    with transaction.atomic():
        qs = Reminder.objects.select_related("policy__vehicle__owner")
        if connection.features.has_select_for_update_skip_locked:
            qs = qs.select_for_update(skip_locked=True, of=("self",))
        r = qs.filter(pk=pk).first()
        # Status transition guard: someone else may have handled it already.
        if r is None or r.status not in (Reminder.PENDING, Reminder.FAILED):
            return "noop"
        if r.status == Reminder.FAILED and (
            r.attempts >= settings.REMINDER_MAX_ATTEMPTS
            or (r.last_attempt_at and timezone.localtime(r.last_attempt_at).date() >= today)
        ):
            return "noop"
        if not r.policy.is_latest:
            r.status, r.error = Reminder.SKIPPED, "Superseded by a newer policy"
            r.save(update_fields=["status", "error"])
            return "skipped"
        r.attempts += 1
        r.last_attempt_at = timezone.now()
        try:
            msg_id = backends.send(r)
        except Exception as e:  # any backend failure is recorded, never crashes the run
            r.status, r.error = Reminder.FAILED, str(e)[:2000]
            r.save()
            if stderr is not None:
                stderr.write(f"FAILED {r}: {e}")
            return "failed"
        r.status, r.error = Reminder.SENT, ""
        r.sent_at = timezone.now()
        r.provider_message_id = msg_id or ""
        r.save()
        return "sent"


def send_due_reminders(today=None, dry_run=False, stdout=None, stderr=None):
    """Send all due reminders (idempotent). Returns counts only; stdout/stderr get human-readable lines."""
    def out(msg):
        if stdout is not None:
            stdout.write(msg)

    today = today or timezone.localdate()
    due = Reminder.objects.filter(
        scheduled_for__lte=today, policy__expiry_date__gte=today
    ).filter(
        Q(status=Reminder.PENDING)
        # failed ones are retried on later runs (not the same day), up to the max attempts
        | Q(status=Reminder.FAILED, attempts__lt=settings.REMINDER_MAX_ATTEMPTS)
    ).exclude(
        status=Reminder.FAILED, last_attempt_at__date=today
    ).order_by("id")

    sent = failed = skipped = 0
    for pk in list(due.values_list("pk", flat=True)):
        if dry_run:
            r = Reminder.objects.select_related("policy__vehicle").get(pk=pk)
            if not r.policy.is_latest:
                out(f"[dry-run] would skip (superseded): {r}")
            else:
                out(f"[dry-run] would send {r.channel} to {r.recipient} "
                    f"({r.policy.vehicle.registration_no}, {r.days_before}d)")
            continue
        result = _process(pk, today, stderr)
        sent += result == "sent"
        failed += result == "failed"
        skipped += result == "skipped"
    n_due = due.count()
    if dry_run:
        out(f"[dry-run] {n_due} reminder(s) due.")
    else:
        out(f"Done: {sent} sent, {failed} failed, {skipped} skipped.")
    return {"date": today.isoformat(), "dry_run": dry_run, "due": n_due,
            "sent": sent, "failed": failed, "skipped": skipped}

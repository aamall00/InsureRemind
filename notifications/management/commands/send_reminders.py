from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from notifications import backends
from notifications.models import Reminder


class Command(BaseCommand):
    help = "Send all due reminders (idempotent; safe to run repeatedly)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would be sent; change nothing.")
        parser.add_argument("--date", help="Treat this YYYY-MM-DD as today.")

    def handle(self, *args, **opts):
        try:
            today = date.fromisoformat(opts["date"]) if opts["date"] else timezone.localdate()
        except ValueError:
            raise CommandError("--date must be YYYY-MM-DD")
        dry = opts["dry_run"]

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
            if dry:
                r = Reminder.objects.select_related("policy__vehicle").get(pk=pk)
                if not r.policy.is_latest:
                    self.stdout.write(f"[dry-run] would skip (superseded): {r}")
                else:
                    self.stdout.write(f"[dry-run] would send {r.channel} to {r.recipient} "
                                      f"({r.policy.vehicle.registration_no}, {r.days_before}d)")
                continue
            result = self._process(pk, today)
            sent += result == "sent"
            failed += result == "failed"
            skipped += result == "skipped"
        if dry:
            self.stdout.write(f"[dry-run] {due.count()} reminder(s) due.")
        else:
            self.stdout.write(f"Done: {sent} sent, {failed} failed, {skipped} skipped.")

    def _process(self, pk, today):
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
                self.stderr.write(f"FAILED {r}: {e}")
                return "failed"
            r.status, r.error = Reminder.SENT, ""
            r.sent_at = timezone.now()
            r.provider_message_id = msg_id or ""
            r.save()
            return "sent"

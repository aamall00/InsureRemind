from datetime import date

from django.core.management.base import BaseCommand, CommandError

from notifications.sending import send_due_reminders


class Command(BaseCommand):
    help = "Send all due reminders (idempotent; safe to run repeatedly)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List what would be sent; change nothing.")
        parser.add_argument("--date", help="Treat this YYYY-MM-DD as today.")

    def handle(self, *args, **opts):
        try:
            today = date.fromisoformat(opts["date"]) if opts["date"] else None
        except ValueError:
            raise CommandError("--date must be YYYY-MM-DD")
        send_due_reminders(today=today, dry_run=opts["dry_run"], stdout=self.stdout, stderr=self.stderr)

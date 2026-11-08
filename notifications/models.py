from django.db import models


class Reminder(models.Model):
    EMAIL, WHATSAPP = "email", "whatsapp"
    CHANNEL_CHOICES = [(EMAIL, "Email"), (WHATSAPP, "WhatsApp")]
    OWNER, AGENT = "owner", "agent"
    RECIPIENT_CHOICES = [(OWNER, "Owner"), (AGENT, "Agent")]
    PENDING, SENT, FAILED, SKIPPED = "pending", "sent", "failed", "skipped"
    STATUS_CHOICES = [(PENDING, "Pending"), (SENT, "Sent"), (FAILED, "Failed"), (SKIPPED, "Skipped")]

    policy = models.ForeignKey("policies.Policy", on_delete=models.CASCADE, related_name="reminders")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES)
    recipient_type = models.CharField(max_length=10, choices=RECIPIENT_CHOICES)
    recipient = models.CharField(max_length=254, blank=True)
    days_before = models.PositiveSmallIntegerField()
    scheduled_for = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    provider_message_id = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scheduled_for", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "channel", "recipient_type", "days_before"],
                name="uniq_reminder_per_policy_channel_recipient_days",
            )
        ]

    def __str__(self):
        return f"{self.channel}/{self.recipient_type} {self.days_before}d for {self.policy_id} [{self.status}]"

from django.contrib import admin

from .models import Reminder


@admin.register(Reminder)
class ReminderAdmin(admin.ModelAdmin):
    list_display = ("policy", "channel", "recipient_type", "recipient", "days_before", "scheduled_for", "status")
    list_filter = ("status", "channel", "recipient_type")

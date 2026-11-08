from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.paginator import Paginator
from django.shortcuts import render
from django.views import View

from .models import Reminder


class ReminderListView(PermissionRequiredMixin, View):
    permission_required = "notifications.view_reminder"
    raise_exception = True

    def get(self, request):
        status = request.GET.get("status", "")
        qs = Reminder.objects.select_related("policy__vehicle__owner").order_by("-scheduled_for", "-id")
        if status in dict(Reminder.STATUS_CHOICES):
            qs = qs.filter(status=status)
        page = Paginator(qs, 25).get_page(request.GET.get("page"))
        return render(request, "notifications/reminder_list.html", {
            "page": page, "status": status, "statuses": Reminder.STATUS_CHOICES,
        })

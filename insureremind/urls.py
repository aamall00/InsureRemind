from django.contrib import admin
from django.urls import include, path

from . import ops_views

urlpatterns = [
    path("healthz", ops_views.healthz, name="healthz"),
    path("internal/cron/send-reminders/", ops_views.cron_send_reminders, name="cron_send_reminders"),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("reminders/", include("notifications.urls")),
    path("", include("policies.urls")),
]

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("reminders/", include("notifications.urls")),
    path("", include("policies.urls")),
]

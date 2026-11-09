from django.contrib import admin

from .models import DocumentAccess, Owner, Policy, PolicyUpload, Vehicle

admin.site.register(Owner)
admin.site.register(Vehicle)
admin.site.register(Policy)
admin.site.register(PolicyUpload)


@admin.register(DocumentAccess)
class DocumentAccessAdmin(admin.ModelAdmin):
    list_display = ("at", "user", "action", "registration_no", "policy_number", "ip_address")
    list_filter = ("action", "at")
    search_fields = ("user__username", "registration_no", "policy_number")
    date_hierarchy = "at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        return request.user.is_active and request.user.is_superuser

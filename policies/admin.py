from django.contrib import admin

from .models import Owner, Policy, PolicyUpload, Vehicle

admin.site.register(Owner)
admin.site.register(Vehicle)
admin.site.register(Policy)
admin.site.register(PolicyUpload)

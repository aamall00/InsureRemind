"""Access levels implemented with Django Groups + model permissions."""
from django.contrib.auth.models import Group

READ_ONLY = "Read only"
READ_WRITE = "Read & Write"
ADMIN = "Admin"
LEVELS = [READ_ONLY, READ_WRITE, ADMIN]


def access_level(user):
    if not user.is_authenticated:
        return ""
    if user.is_superuser:
        return ADMIN
    names = set(user.groups.values_list("name", flat=True))
    for level in (ADMIN, READ_WRITE, READ_ONLY):
        if level in names:
            return level
    return "No access"


def set_access_level(user, level):
    if level not in LEVELS:
        raise ValueError(f"Unknown access level {level!r}")
    user.groups.remove(*Group.objects.filter(name__in=LEVELS))
    user.groups.add(Group.objects.get(name=level))
    is_admin = level == ADMIN
    user.is_staff = is_admin
    user.is_superuser = is_admin
    user.save(update_fields=["is_staff", "is_superuser"])

"""Create the three access-level groups and assign model permissions."""
from django.contrib.auth.management import create_permissions
from django.db import migrations

DOMAIN_MODELS = [
    ("policies", "owner"), ("policies", "vehicle"), ("policies", "policy"),
    ("policies", "policyupload"), ("notifications", "reminder"),
]


def create_groups(apps, schema_editor):
    # Permissions are normally created post-migrate; create them now so we can assign them.
    for label in ("policies", "notifications", "accounts", "auth"):
        app_config = apps.get_app_config(label)
        app_config.models_module = True  # historical AppConfigStub lacks this attribute
        create_permissions(app_config, verbosity=0, apps=apps)
        app_config.models_module = None

    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")

    def perms(actions, models=DOMAIN_MODELS):
        q = Permission.objects.none()
        out = []
        for app, model in models:
            codes = [f"{a}_{model}" for a in actions]
            out += list(Permission.objects.filter(content_type__app_label=app, codename__in=codes))
        return out

    read_only = perms(["view"])
    read_write = perms(["view", "add", "change"])
    admin = list(Permission.objects.filter(
        content_type__app_label__in=["policies", "notifications", "accounts"])) + list(
        Permission.objects.filter(content_type__app_label="auth", content_type__model__in=["user", "group"]))

    for name, plist in (("Read only", read_only), ("Read & Write", read_write), ("Admin", admin)):
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.set(plist)


def remove_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name__in=["Read only", "Read & Write", "Admin"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("policies", "0001_initial"),
        ("notifications", "0001_initial"),
    ]
    operations = [migrations.RunPython(create_groups, remove_groups)]

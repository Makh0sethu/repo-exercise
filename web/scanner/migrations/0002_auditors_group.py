"""Create the "Sentinel auditors" group: staff who may read and export scan logs."""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUP = "Sentinel auditors"
PERMS = ["view_scanlog", "export_scanlog"]


def create_group(apps, schema_editor):
    # Permissions are normally created after all migrations; make sure they exist now.
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    group, _ = Group.objects.get_or_create(name=GROUP)
    group.permissions.set(Permission.objects.filter(content_type__app_label="scanner", codename__in=PERMS))


def remove_group(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name=GROUP).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("scanner", "0001_initial"),
        ("auth", "__latest__"),
        ("contenttypes", "__latest__"),
    ]
    operations = [migrations.RunPython(create_group, remove_group)]

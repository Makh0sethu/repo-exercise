"""Create the permission groups the GitHub roles map onto.

operator: see targets and runs, start runs.
auditor:  read everything (runs, findings, audit trail, scan logs), export, verify. Cannot start runs.
admin:    a superuser; not a group.
"""
from django.contrib.auth.management import create_permissions
from django.db import migrations

GROUPS = {
    "Sentinel operators": ["pipeline.view_target", "pipeline.view_run", "pipeline.add_run",
                           "pipeline.view_step", "pipeline.view_finding"],
    "Sentinel auditors": ["pipeline.view_target", "pipeline.view_run", "pipeline.view_step", "pipeline.view_finding",
                          "audit.view_auditevent", "audit.verify_auditevent", "audit.export_auditevent",
                          "audit.view_evidenceartifact", "scanner.view_scanlog", "scanner.export_scanlog"],
}


def create_groups(apps, schema_editor):
    for app_config in apps.get_app_configs():
        app_config.models_module = True
        create_permissions(app_config, apps=apps, verbosity=0)
        app_config.models_module = None
    Group, Permission = apps.get_model("auth", "Group"), apps.get_model("auth", "Permission")
    for name, perms in GROUPS.items():
        group, _ = Group.objects.get_or_create(name=name)
        found = []
        for p in perms:
            app, codename = p.split(".")
            found.append(Permission.objects.get(content_type__app_label=app, codename=codename))
        group.permissions.set(found)


def remove_groups(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name="Sentinel operators").delete()


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial"), ("pipeline", "0001_initial"), ("scanner", "0002_auditors_group"),
                    ("auth", "__latest__"), ("contenttypes", "__latest__")]
    operations = [migrations.RunPython(create_groups, remove_groups)]

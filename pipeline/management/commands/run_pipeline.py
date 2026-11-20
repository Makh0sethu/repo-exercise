from django.core.management.base import BaseCommand

from pipeline.models import Target
from pipeline.runner import create_run, execute


class Command(BaseCommand):
    help = "Run the security pipeline against a target: run_pipeline <name> [--add URL]"

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--add", metavar="GIT_URL", help="Create the target first")
        parser.add_argument("--ref", default="HEAD")
        parser.add_argument("--private", action="store_true")
        parser.add_argument("--by", default="cli")

    def handle(self, *a, name, add, ref, private, by, **o):
        if add:
            target, _ = Target.objects.update_or_create(name=name, defaults={"git_url": add, "ref": ref, "private": private, "created_by": by})
        else:
            target = Target.objects.get(name=name)
        run = execute(create_run(target, requested_by=by))
        for s in run.steps.all():
            self.stdout.write(f"  [{s.status:6}] {s.tool}: {s.summary}")
        self.stdout.write(f"Run {run.pk}: {run.status} at {run.commit_sha[:10] or '-'}, {run.findings.count()} findings. Audit: manage.py verify_audit")

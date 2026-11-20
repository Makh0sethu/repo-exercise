from django.core.management.base import BaseCommand

from audit.log import verify_chain
from audit.models import AuditEvent


class Command(BaseCommand):
    help = "Verify the audit hash chain. Exit code 1 if it has been tampered with."

    def handle(self, *args, **opts):
        total = AuditEvent.objects.count()
        ok, errors = verify_chain()
        if ok:
            self.stdout.write(self.style.SUCCESS(f"Audit chain intact: {total} events verified."))
            return
        self.stderr.write(self.style.ERROR(f"Audit chain BROKEN ({len(errors)} problem(s)):"))
        for seq, reason in errors:
            self.stderr.write(f"  event #{seq}: {reason}")
        raise SystemExit(1)

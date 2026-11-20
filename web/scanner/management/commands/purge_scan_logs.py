from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from scanner.models import ScanLog


class Command(BaseCommand):
    help = "Delete scan logs older than SENTINEL_LOG_RETENTION_DAYS (default 30). Run it daily."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=settings.SENTINEL_LOG_RETENTION_DAYS)

    def handle(self, *args, days, **opts):
        cutoff = timezone.now() - timedelta(days=days)
        deleted, _ = ScanLog.objects.filter(created_at__lt=cutoff).delete()
        self.stdout.write(f"Deleted {deleted} scan logs older than {days} days.")

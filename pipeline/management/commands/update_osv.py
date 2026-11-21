from django.core.management.base import BaseCommand

from pipeline import osvlocal


class Command(BaseCommand):
    help = ("Download the public OSV vulnerability dumps and build the local index used by offline runs "
            "(SENTINEL_OFFLINE=1). This is the only command that needs the internet; run it on a schedule.")

    def add_arguments(self, parser):
        parser.add_argument("--eco", nargs="+", choices=osvlocal.ECOSYSTEMS, default=list(osvlocal.ECOSYSTEMS))

    def handle(self, *a, eco, **o):
        counts = osvlocal.update(eco, progress=self.stdout.write)
        m = osvlocal.meta()
        self.stdout.write(self.style.SUCCESS(f"Offline database updated {m['updated_at']}: {counts}. Stored in {osvlocal.data_dir()}"))

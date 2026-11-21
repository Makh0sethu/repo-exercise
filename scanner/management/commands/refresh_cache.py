"""Management command to refresh cached public vulnerability & news feeds."""
from __future__ import annotations

from django.core.management.base import BaseCommand

from scanner import store
from scanner.http_client import STALE_LIMIT, clear_cache
from scanner.news import fetch_all_rss_stories
from scanner.sources import fetch_cisa_kev, fetch_retirejs_repo


class Command(BaseCommand):
    help = "Refresh cached public feeds (CISA KEV, RetireJS, and security news RSS)."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear existing cache entries before fetching fresh data.",
        )

    def handle(self, *args, **options) -> None:
        if options["clear"]:
            clear_cache()
            self.stdout.write("Cleared cache.")

        try:
            kev = fetch_cisa_kev()
            self.stdout.write(self.style.SUCCESS(f"CISA KEV: {len(kev)} exploited CVEs cached."))
        except Exception as exc:
            self.stderr.write(self.style.WARNING(f"CISA KEV failed: {exc}"))

        try:
            repo = fetch_retirejs_repo()
            self.stdout.write(self.style.SUCCESS(f"RetireJS: {len(repo)} libraries cached."))
        except Exception as exc:
            self.stderr.write(self.style.WARNING(f"RetireJS failed: {exc}"))

        try:
            stories, ok = fetch_all_rss_stories(days=30)
            status = self.style.SUCCESS if ok else self.style.WARNING
            self.stdout.write(status(f"News RSS: {len(stories)} recent stories cached."))
        except Exception as exc:
            self.stderr.write(self.style.WARNING(f"News RSS failed: {exc}"))

        removed = store.purge(STALE_LIMIT)
        info = store.stats()
        self.stdout.write(f"Saved copies: {info['with_data']} with data" + (f", {removed} too old removed." if removed else "."))

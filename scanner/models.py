from django.db import models


class ScanLog(models.Model):
    """One row per scan attempt: who, when, what was scanned and how it went.

    Uploaded file contents are never stored, only the package list parsed from
    them. Rows older than SENTINEL_LOG_RETENTION_DAYS are removed by
    `manage.py purge_scan_logs`.
    """
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    request_id = models.CharField(max_length=36, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user_agent = models.CharField(max_length=300, blank=True)
    status_code = models.PositiveSmallIntegerField()
    outcome = models.CharField(max_length=20, db_index=True)  # ok, too_large, empty, error
    files = models.JSONField(default=list)          # [{"name", "kind", "count"}]
    package_count = models.PositiveIntegerField(default=0)
    packages = models.JSONField(default=list)       # ["PyPI:django@4.2.1", ...]
    unreadable = models.JSONField(default=list)     # lines that couldn't be parsed
    sources = models.JSONField(default=dict)        # {"OSV.dev": "ok", ...}
    vulnerable_count = models.PositiveIntegerField(default=0)
    problem_count = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("export_scanlog", "Can export scan logs")]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.ip} {self.outcome} ({self.package_count} packages)"

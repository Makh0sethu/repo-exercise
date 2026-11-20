from django.conf import settings
from django.db import models


class Target(models.Model):
    """An app under test. v1 supports git repositories; images come later."""
    name = models.CharField(max_length=100, unique=True)
    git_url = models.URLField(max_length=300)
    ref = models.CharField(max_length=100, default="HEAD", help_text="Branch or tag. HEAD = default branch.")
    private = models.BooleanField(default=False, help_text="Needs GitHub credentials to clone.")
    created_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Run(models.Model):
    """One execution of a pipeline against one version of a target. The exact
    pipeline definition is stored with the run so you can see what it did."""
    STATUS = [("pending", "Pending"), ("running", "Running"), ("passed", "Passed"),
              ("failed", "Failed"), ("error", "Error")]
    target = models.ForeignKey(Target, on_delete=models.PROTECT, related_name="runs")
    status = models.CharField(max_length=10, choices=STATUS, default="pending")
    pipeline = models.JSONField(help_text="The exact step list that was run")
    commit_sha = models.CharField(max_length=40, blank=True)
    requested_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Run {self.pk} · {self.target} · {self.status}"


class Step(models.Model):
    """One tool invocation: what ran, why, the exact command, and its evidence."""
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="steps")
    order = models.PositiveSmallIntegerField()
    tool = models.CharField(max_length=60)
    reason = models.CharField(max_length=300)
    argv = models.JSONField(default=list, help_text="Exact command; secrets are never included")
    status = models.CharField(max_length=10, default="pending")
    exit_code = models.IntegerField(null=True)
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    stdout_sha256 = models.CharField(max_length=64, blank=True)
    stderr_sha256 = models.CharField(max_length=64, blank=True)
    summary = models.JSONField(default=dict)

    class Meta:
        ordering = ["run", "order"]


class Finding(models.Model):
    run = models.ForeignKey(Run, on_delete=models.CASCADE, related_name="findings")
    step = models.ForeignKey(Step, on_delete=models.CASCADE, related_name="findings")
    severity = models.CharField(max_length=10, db_index=True)
    title = models.CharField(max_length=300)
    location = models.CharField(max_length=300, blank=True)
    detail = models.JSONField(default=dict)
    evidence_sha256 = models.CharField(max_length=64, blank=True, help_text="Raw output that proves it")

    class Meta:
        ordering = ["run", "step"]

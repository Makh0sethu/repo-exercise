from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.identity import actor_name
from audit.log import record
from scanner.errors import forbidden

from . import jobs
from .models import Run, Target
from .runner import create_run


@login_required(login_url="/auth/github/login/")
def runs_page(request):
    if not request.user.has_perm("pipeline.view_run"):
        record("auth.denied", actor=actor_name(request.user), payload={"reason": "no pipeline.view_run", "path": request.path})
        return forbidden(request, "You don't have permission to view security runs. Ask an admin for the operator or auditor role.")
    runs = Run.objects.select_related("target").prefetch_related("steps", "findings")[:50]
    return render(request, "pipeline/runs.html", {
        "runs": runs, "targets": Target.objects.all(),
        "can_run": request.user.has_perm("pipeline.add_run"), "who": actor_name(request.user)})


@login_required(login_url="/auth/github/login/")
@require_POST
def start_run(request):
    if not request.user.has_perm("pipeline.add_run"):
        record("auth.denied", actor=actor_name(request.user), payload={"reason": "no pipeline.add_run", "path": request.path})
        return forbidden(request, "You don't have permission to start runs. Only operators and admins can; auditors are read-only.")
    target = get_object_or_404(Target, pk=request.POST.get("target"))
    run = create_run(target, requested_by=actor_name(request.user))
    jobs.start_async(run)
    return redirect("pipeline:runs")


SEV_ORDER = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "unrated": 4}


def _can_view(request):
    if not request.user.has_perm("pipeline.view_run"):
        record("auth.denied", actor=actor_name(request.user), payload={"reason": "no pipeline.view_run", "path": request.path})
        return False
    return True


@login_required(login_url="/auth/github/login/")
def run_detail(request, pk):
    if not _can_view(request):
        return forbidden(request, "You don't have permission to view security runs.")
    run = get_object_or_404(Run.objects.select_related("target"), pk=pk)
    findings = sorted(run.findings.select_related("step"), key=lambda f: (SEV_ORDER.get(f.severity, 9), f.step.order, f.title))
    counts = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return render(request, "pipeline/run_detail.html", {
        "run": run, "steps": run.steps.all(), "findings": findings, "counts": counts,
        "running": run.status in ("pending", "running")})


@login_required(login_url="/auth/github/login/")
def run_status(request, pk):
    """Polled by the run page so it can refresh itself once, when the run finishes."""
    if not _can_view(request):
        return JsonResponse({"error": "forbidden"}, status=403)
    run = get_object_or_404(Run, pk=pk)
    return JsonResponse({"status": run.status, "steps": run.steps.count(), "done": run.status not in ("pending", "running")})

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.identity import actor_name
from audit.log import record, verify_chain
from audit.log import read_evidence
from audit.models import AuditEvent, EvidenceArtifact
from scanner.errors import forbidden

from . import jobs
from .access import actor, has_perm, login_unless_open
from .gitsafe import UnsafeTarget, check_git_url, check_ref
from .models import Run, Target
from .runner import create_run


@login_unless_open
def runs_page(request):
    if not has_perm(request, "pipeline.view_run"):
        record("auth.denied", actor=actor(request, actor_name(request.user)), payload={"reason": "no pipeline.view_run", "path": request.path})
        return forbidden(request, "You don't have permission to view security runs. Ask an admin for the operator or auditor role.")
    runs = Run.objects.select_related("target").prefetch_related("steps", "findings")[:50]
    return render(request, "pipeline/runs.html", {
        "runs": runs, "targets": Target.objects.all(),
        "can_run": has_perm(request, "pipeline.add_run"), "can_add_target": has_perm(request, "pipeline.add_target"), "who": actor(request, actor_name(request.user))})


@login_unless_open
@require_POST
def start_run(request):
    if not has_perm(request, "pipeline.add_run"):
        record("auth.denied", actor=actor(request, actor_name(request.user)), payload={"reason": "no pipeline.add_run", "path": request.path})
        return forbidden(request, "You don't have permission to start runs. Only operators and admins can; auditors are read-only.")
    target = get_object_or_404(Target, pk=request.POST.get("target"))
    run = create_run(target, requested_by=actor(request, actor_name(request.user)))
    jobs.start_async(run)
    return redirect("pipeline:runs")


@login_unless_open
@require_POST
def add_target(request):
    who = actor(request, actor_name(request.user))
    if not has_perm(request, "pipeline.add_target"):
        record("auth.denied", actor=who, payload={"reason": "no pipeline.add_target", "path": request.path})
        return forbidden(request, "You don't have permission to add targets.")
    name = request.POST.get("name", "").strip()
    url = request.POST.get("git_url", "").strip()
    ref = request.POST.get("ref", "").strip() or "HEAD"
    private = request.POST.get("private") == "on"
    try:
        if not name or len(name) > 100:
            raise UnsafeTarget("Give the target a name (100 characters or fewer)")
        if Target.objects.filter(name=name).exists():
            raise UnsafeTarget(f"A target named {name!r} already exists")
        check_git_url(url)
        check_ref(ref)
    except UnsafeTarget as e:
        messages.error(request, str(e))
        return redirect("pipeline:runs")
    t = Target.objects.create(name=name, git_url=url, ref=ref, private=private, created_by=who)
    record("target.created", actor=who, payload={"target": t.name, "git_url": url, "ref": ref, "private": private})
    messages.success(request, f"Target {t.name!r} added.")
    return redirect("pipeline:runs")


@login_unless_open
def audit_page(request):
    if not has_perm(request, "audit.view_auditevent"):
        record("auth.denied", actor=actor(request, actor_name(request.user)), payload={"reason": "no audit.view_auditevent", "path": request.path})
        return forbidden(request, "You don't have permission to view the audit trail.")
    ok, errors = verify_chain()
    events = AuditEvent.objects.order_by("-seq")[:200]
    return render(request, "pipeline/audit.html", {
        "events": events, "total": AuditEvent.objects.count(), "intact": ok, "errors": errors[:20],
        "who": actor(request, actor_name(request.user))})


def _evidence_text(sha, limit=4000):
    """Captured tool output, re-checked against its recorded hash; never raises into the page."""
    if not sha:
        return ""
    try:
        text = read_evidence(EvidenceArtifact.objects.get(sha256=sha)).decode("utf-8", "replace")
    except Exception as e:  # missing or altered evidence is itself worth showing
        return f"(evidence unavailable: {e})"
    return text[:limit] + ("\n... truncated" if len(text) > limit else "")


SEV_ORDER = {"critical": 0, "high": 1, "moderate": 2, "low": 3, "unrated": 4}


def _can_view(request):
    if not has_perm(request, "pipeline.view_run"):
        record("auth.denied", actor=actor(request, actor_name(request.user)), payload={"reason": "no pipeline.view_run", "path": request.path})
        return False
    return True


@login_unless_open
def run_detail(request, pk):
    if not _can_view(request):
        return forbidden(request, "You don't have permission to view security runs.")
    run = get_object_or_404(Run.objects.select_related("target"), pk=pk)
    findings = sorted(run.findings.select_related("step"), key=lambda f: (SEV_ORDER.get(f.severity, 9), f.step.order, f.title))
    counts = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    steps = list(run.steps.all())
    for st in steps:
        st.stderr_text, st.stdout_text = _evidence_text(st.stderr_sha256), _evidence_text(st.stdout_sha256, 1500)
    return render(request, "pipeline/run_detail.html", {
        "run": run, "steps": steps, "findings": findings, "counts": counts,
        "running": run.status in ("pending", "running")})


@login_unless_open
def run_status(request, pk):
    """Polled by the run page so it can refresh itself once, when the run finishes."""
    if not _can_view(request):
        return JsonResponse({"error": "forbidden"}, status=403)
    run = get_object_or_404(Run, pk=pk)
    return JsonResponse({"status": run.status, "steps": run.steps.count(), "done": run.status not in ("pending", "running")})

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.identity import actor_name
from audit.log import record

from . import jobs
from .models import Run, Target
from .runner import create_run


@login_required(login_url="/auth/github/login/")
def runs_page(request):
    if not request.user.has_perm("pipeline.view_run"):
        record("auth.denied", actor=actor_name(request.user), payload={"reason": "no pipeline.view_run", "path": request.path})
        return HttpResponseForbidden("You don't have permission to view runs.")
    runs = Run.objects.select_related("target").prefetch_related("steps", "findings")[:50]
    return render(request, "pipeline/runs.html", {
        "runs": runs, "targets": Target.objects.all(),
        "can_run": request.user.has_perm("pipeline.add_run"), "who": actor_name(request.user)})


@login_required(login_url="/auth/github/login/")
@require_POST
def start_run(request):
    if not request.user.has_perm("pipeline.add_run"):
        record("auth.denied", actor=actor_name(request.user), payload={"reason": "no pipeline.add_run", "path": request.path})
        return HttpResponseForbidden("You don't have permission to start runs.")
    target = get_object_or_404(Target, pk=request.POST.get("target"))
    run = create_run(target, requested_by=actor_name(request.user))
    jobs.start_async(run)
    return redirect("pipeline:runs")

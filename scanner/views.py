import logging
import time

from django.conf import settings
from django.http import JsonResponse
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import headlines, history
from .logos import logo_for
from .models import ScanLog
from .parsers import parse_inputs
from .scan import run_scan


log = logging.getLogger(__name__)


class TooLarge(Exception):
    pass


class InvalidRequest(Exception):
    pass


def _parse_content_length(raw):
    if not raw:
        return 0
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest("Invalid Content-Length header.") from exc
    if value < 0:
        raise InvalidRequest("Negative Content-Length header.")
    return value


def _log_scan(request, started, status, outcome, parsed=None, result=None, error=""):
    """Record who scanned what, and when. File contents are never stored."""
    try:
        ScanLog.objects.create(
            request_id=getattr(request, "request_id", ""),
            ip=getattr(request, "client_ip", None) or request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
            status_code=status,
            outcome=outcome,
            files=[{"name": n, "kind": k, "count": c} for n, k, c in parsed.files] if parsed else [],
            package_count=len(parsed.packages) if parsed else 0,
            packages=[f"{p.ecosystem}:{p.name}@{p.version or p.spec or '*'}" for p in parsed.packages] if parsed else [],
            unreadable=[line for _, line in parsed.unreadable][:200] if parsed else [],
            sources={s["name"]: s["status"] for s in result["sources"]} if result else {},
            vulnerable_count=result["stats"]["vulnerable"] if result else 0,
            problem_count=result["stats"]["problems"] if result else 0,
            duration_ms=round((time.monotonic() - started) * 1000),
            error=error[:2000],
        )
    except Exception:  # logging must never break a scan
        log.exception("Could not write scan log")


def _read_inputs(request):
    """Read the form. Files stay in memory; only the parsed package list is logged."""
    # Check the raw size first: Django silently drops files over the memory limit.
    if _parse_content_length(request.META.get("CONTENT_LENGTH")) > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
        raise TooLarge()
    files = request.FILES.getlist("files")
    pasted = request.POST.get("pasted", "")
    other = request.POST.get("other", "")
    total = sum(f.size for f in files) + len(pasted.encode()) + len(other.encode())
    if total > settings.SENTINEL_MAX_UPLOAD:
        raise TooLarge()
    uploads = [(f.name, f.read().decode("utf-8", errors="replace")) for f in files]
    return parse_inputs(uploads, pasted, other)


def _form_values(request):
    """What the person typed, so an error never throws their input away."""
    return {"pasted": request.POST.get("pasted", ""), "other": request.POST.get("other", "")}


@require_GET
def home(request):
    """Landing page: what Sentinel does, then the latest security news."""
    return render(request, "scanner/home.html", {"h": headlines.latest()})


@require_http_methods(["GET", "POST"])
def scan_entry(request):
    """/scan/ shows the form on GET and runs the scan on POST."""
    return run_scan_view(request) if request.method == "POST" else scan_page(request)


@require_GET
def scan_page(request):
    return render(request, "scanner/scan.html", {"form": {"pasted": "", "other": ""}})


@require_POST
def parse_preview(request):
    """What the scan page shows under "Detected packages" before scanning."""
    try:
        parsed = _read_inputs(request)
    except InvalidRequest:
        return JsonResponse({"error": "Invalid request."}, status=400)
    except TooLarge:
        return JsonResponse({"error": "Files are larger than 1 MB."}, status=413)
    return JsonResponse({
        "packages": [{"ecosystem": p.ecosystem, "name": p.name, "version": p.version,
                      "spec": p.spec, "logo": logo_for(p.ecosystem, p.name)[0]}
                     for p in parsed.packages],
        "files": [{"name": n, "kind": k, "count": c} for n, k, c in parsed.files],
        "unreadable": [{"source": s, "line": l} for s, l in parsed.unreadable],
    })


@require_POST
def run_scan_view(request):
    started = time.monotonic()
    try:
        parsed = _read_inputs(request)
    except InvalidRequest:
        _log_scan(request, started, 400, "invalid")
        return render(request, "scanner/scan.html", {"error": "Invalid request.", "form": _form_values(request)}, status=400)
    except TooLarge:
        _log_scan(request, started, 413, "too_large")
        return render(request, "scanner/scan.html", {"error": "Files are larger than 1 MB.", "form": _form_values(request)}, status=413)
    if not parsed.packages:
        _log_scan(request, started, 400, "empty", parsed)
        return render(request, "scanner/scan.html", {
            "error": "No packages found. Upload a dependency file or paste its contents.",
            "unreadable": parsed.unreadable, "form": _form_values(request)}, status=400)
    try:
        result = run_scan(parsed.packages)
    except Exception as exc:
        _log_scan(request, started, 500, "error", parsed, error=repr(exc))
        raise
    result["inputs"] = parsed.files
    result["unreadable"] = parsed.unreadable
    result["request_id"] = getattr(request, "request_id", "")
    _log_scan(request, started, 200, "ok", parsed, result)
    # Post/Redirect/Get: the result has its own address, so Back, Refresh and bookmarks all work
    # and refreshing never re-runs (or re-logs) the scan.
    return redirect(reverse("scanner:result", args=[history.save(request, result)]))


@require_GET
def result_page(request, rid):
    result = history.load(request, rid)
    if result is None:
        raise Http404("That result is no longer available. Results are kept only for this browser session.")
    return render(request, "scanner/results.html", {"r": result, "rid": rid})


@require_GET
def results_list(request):
    return render(request, "scanner/results_list.html", {"items": history.recent(request)})


@require_GET
def about(request):
    """What Sentinel is, with the full site map and code architecture."""
    return render(request, "scanner/about.html")


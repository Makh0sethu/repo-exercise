from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from .demo import demo_result
from .logos import logo_for
from .parsers import parse_inputs
from .scan import run_scan


class TooLarge(Exception):
    pass


def _read_inputs(request):
    """Read the form. Files stay in memory and are never saved."""
    # Check the raw size first: Django silently drops files over the memory limit.
    if int(request.META.get("CONTENT_LENGTH") or 0) > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
        raise TooLarge()
    files = request.FILES.getlist("files")
    pasted = request.POST.get("pasted", "")
    other = request.POST.get("other", "")
    total = sum(f.size for f in files) + len(pasted.encode()) + len(other.encode())
    if total > settings.VULNWATCH_MAX_UPLOAD:
        raise TooLarge()
    uploads = [(f.name, f.read().decode("utf-8", errors="replace")) for f in files]
    return parse_inputs(uploads, pasted, other)


@require_GET
def scan_page(request):
    return render(request, "scanner/scan.html")


@require_POST
def parse_preview(request):
    """What the scan page shows under "Detected packages" before scanning."""
    try:
        parsed = _read_inputs(request)
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
    try:
        parsed = _read_inputs(request)
    except TooLarge:
        return render(request, "scanner/scan.html", {"error": "Files are larger than 1 MB."}, status=413)
    if not parsed.packages:
        return render(request, "scanner/scan.html", {
            "error": "No packages found. Upload a dependency file or paste its contents.",
            "unreadable": parsed.unreadable}, status=400)
    result = run_scan(parsed.packages)
    result["inputs"] = parsed.files
    result["unreadable"] = parsed.unreadable
    return render(request, "scanner/results.html", {"r": result})


@require_GET
def demo(request):
    result = demo_result()
    result["inputs"] = [("requirements.txt", "requirements.txt", 9), ("package.json", "package.json", 3),
                        ("Other software", "other", 2)]
    result["unreadable"] = []
    return render(request, "scanner/results.html", {"r": result})

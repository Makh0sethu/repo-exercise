import csv

from django.contrib import admin
from django.http import HttpResponse

from .models import ScanLog


@admin.register(ScanLog)
class ScanLogAdmin(admin.ModelAdmin):
    """Read-only audit trail. Only superusers may delete rows."""
    list_display = ("created_at", "ip", "outcome", "status_code", "package_count",
                    "short_packages", "vulnerable_count", "duration_ms", "short_agent")
    list_filter = ("outcome", "status_code", "created_at")
    search_fields = ("ip", "request_id", "user_agent", "packages")
    date_hierarchy = "created_at"
    actions = ["export_csv"]
    readonly_fields = [f.name for f in ScanLog._meta.fields]

    @admin.display(description="packages")
    def short_packages(self, obj):
        shown = ", ".join(obj.packages[:4])
        return shown + (f" +{len(obj.packages) - 4} more" if len(obj.packages) > 4 else "")

    @admin.display(description="browser")
    def short_agent(self, obj):
        return obj.user_agent[:60]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

    @admin.action(description="Export selected to CSV", permissions=["export"])
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="sentinel-scan-logs.csv"'
        w = csv.writer(response)
        cols = ["created_at", "request_id", "ip", "outcome", "status_code", "package_count",
                "vulnerable_count", "problem_count", "duration_ms", "user_agent", "packages", "error"]
        w.writerow(cols)
        for row in queryset:
            w.writerow([" ".join(getattr(row, c)) if c == "packages" else getattr(row, c) for c in cols])
        return response

    def has_export_permission(self, request):
        return request.user.has_perm("scanner.export_scanlog")

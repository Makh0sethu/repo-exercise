import csv

from django.contrib import admin
from django.http import HttpResponse

from .log import verify_chain
from .models import AuditEvent, EvidenceArtifact


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    """Read-only. Nobody edits or deletes audit events through the admin;
    doing so would break the chain by design."""
    list_display = ("seq", "at", "actor", "action", "object_type", "object_id", "chain_ok")
    list_filter = ("action", "actor", "at")
    search_fields = ("actor", "action", "object_type", "object_id", "this_hash")
    date_hierarchy = "at"
    readonly_fields = [f.name for f in AuditEvent._meta.fields]
    actions = ["export_csv"]

    @admin.display(boolean=True, description="hash ok")
    def chain_ok(self, obj):
        return obj.compute_hash() == obj.this_hash

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Export selected to CSV", permissions=["export"])
    def export_csv(self, request, queryset):
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="audit-trail.csv"'
        w = csv.writer(resp)
        cols = ["seq", "at", "actor", "action", "object_type", "object_id", "payload", "prev_hash", "this_hash"]
        w.writerow(cols)
        for ev in queryset.order_by("seq"):
            w.writerow([getattr(ev, c) for c in cols])
        return resp

    def has_export_permission(self, request):
        return request.user.has_perm("audit.export_auditevent")


@admin.register(EvidenceArtifact)
class EvidenceArtifactAdmin(admin.ModelAdmin):
    list_display = ("sha256", "label", "size", "content_type", "created_at")
    search_fields = ("sha256", "label")
    readonly_fields = [f.name for f in EvidenceArtifact._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

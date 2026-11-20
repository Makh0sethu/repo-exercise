from django.contrib import admin

from .models import Finding, Run, Step, Target


class ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


class StepInline(admin.TabularInline):
    model = Step
    extra = 0
    can_delete = False
    fields = ("order", "tool", "reason", "status", "exit_code", "argv", "stdout_sha256")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None): return False


@admin.register(Target)
class TargetAdmin(admin.ModelAdmin):
    list_display = ("name", "git_url", "ref", "private", "created_by")


@admin.register(Run)
class RunAdmin(ReadOnly):
    list_display = ("id", "target", "status", "commit_sha", "requested_by", "created_at")
    list_filter = ("status", "target")
    inlines = [StepInline]


@admin.register(Finding)
class FindingAdmin(ReadOnly):
    list_display = ("severity", "title", "location", "run")
    list_filter = ("severity",)
    search_fields = ("title", "location")

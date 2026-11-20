from django.conf import settings
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Sentinel admin"
admin.site.site_title = "Sentinel"

urlpatterns = [
    path(settings.SENTINEL_ADMIN_PATH, admin.site.urls),
    path("", include("scanner.urls")),
]

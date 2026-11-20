from django.urls import path

from . import views

app_name = "scanner"
urlpatterns = [
    path("", views.scan_page, name="scan"),
    path("scan/", views.run_scan_view, name="run"),
    path("parse/", views.parse_preview, name="parse"),
    path("demo/", views.demo, name="demo"),
]

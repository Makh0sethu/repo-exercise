from django.urls import path

from . import views

app_name = "scanner"
urlpatterns = [
    path("", views.scan_page, name="scan"),
    path("scan/", views.run_scan_view, name="run"),
    path("parse/", views.parse_preview, name="parse"),
    path("results/", views.results_list, name="results"),
    path("results/<str:rid>/", views.result_page, name="result"),
]

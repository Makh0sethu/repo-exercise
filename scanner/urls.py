from django.urls import path

from . import views

app_name = "scanner"
urlpatterns = [
    path("", views.home, name="home"),
    path("scan/", views.scan_entry, name="scan"),
    path("scan/", views.scan_entry, name="run"),        # same address: GET shows the form, POST runs it
    path("parse/", views.parse_preview, name="parse"),
    path("results/", views.results_list, name="results"),
    path("results/<str:rid>/", views.result_page, name="result"),
    path("about/", views.about, name="about"),
]

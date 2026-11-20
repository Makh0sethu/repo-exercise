from django.urls import path

from . import views

app_name = "pipeline"
urlpatterns = [
    path("", views.runs_page, name="runs"),
    path("start/", views.start_run, name="start"),
    path("runs/<int:pk>/", views.run_detail, name="run"),
    path("runs/<int:pk>/status/", views.run_status, name="run_status"),
]

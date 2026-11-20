from django.urls import path

from . import views

app_name = "accounts"
urlpatterns = [
    path("github/login/", views.github_login, name="login"),
    path("github/callback/", views.github_callback, name="callback"),
    path("logout/", views.github_logout, name="logout"),
]

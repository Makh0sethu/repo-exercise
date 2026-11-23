"""URLs for public-only mode (SENTINEL_PUBLIC_ONLY=1): the scanner, results, news and About pages.

The Django admin, GitHub sign-in (/auth/) and Security runs (/server/) are deliberately not
routed here, so they answer 404 instead of a login page that would show they exist.
"""
from django.urls import include, path

urlpatterns = [
    path("", include("scanner.urls")),
]

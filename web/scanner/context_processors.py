from django.conf import settings


def sentinel(request):
    return {"retention_days": settings.SENTINEL_LOG_RETENTION_DAYS}

"""Operational endpoints: health check and the token-protected daily reminder trigger."""
import logging
import secrets

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from notifications.sending import send_due_reminders

logger = logging.getLogger(__name__)


@login_not_required
@require_GET
def healthz(request):
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        logger.exception("healthz: database check failed")
        return HttpResponse("db error", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


@login_not_required
@csrf_exempt
@require_POST
def cron_send_reminders(request):
    token = settings.CRON_TOKEN
    if not token:
        raise Http404()
    given = request.headers.get("X-Cron-Token", "")
    if not secrets.compare_digest(given.encode(), token.encode()):
        return HttpResponseForbidden("forbidden")
    dry = request.GET.get("dry_run") == "1"
    logger.info("cron send-reminders started (dry_run=%s)", dry)
    try:
        summary = send_due_reminders(dry_run=dry)
    except Exception:
        logger.exception("cron send-reminders failed")
        return JsonResponse({"error": "failed"}, status=500)
    logger.info("cron send-reminders finished: %s", summary)
    return JsonResponse(summary)

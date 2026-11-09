import ipaddress
import logging

from django.db import transaction

from .models import DocumentAccess

logger = logging.getLogger(__name__)


def _valid_ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _clean_ip(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("["):  # [::1]:443 or [::1]
        return _valid_ip(raw[1:].split("]", 1)[0])
    if raw.count(":") == 1:  # 1.2.3.4:5678
        raw = raw.split(":", 1)[0]
    return _valid_ip(raw)


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = _clean_ip(forwarded.split(",")[0]) if forwarded else None
    return ip or _clean_ip(request.META.get("REMOTE_ADDR"))


def record_access(request, action, *, policy=None, vehicle=None, upload=None):
    """Write one audit row. Never raises: an audit failure must not break the download."""
    try:
        vehicle = vehicle or (policy.vehicle if policy else None)
        reg = vehicle.registration_no if vehicle else (upload.vehicle_no if upload else "")
        with transaction.atomic():
            return DocumentAccess.objects.create(
                user=request.user if request.user.is_authenticated else None,
                action=action, policy=policy, vehicle=vehicle, upload=upload,
                registration_no=reg, policy_number=policy.policy_number if policy else "",
                ip_address=client_ip(request),
            )
    except Exception:
        logger.exception("Failed to record document access (%s)", action)
        return None

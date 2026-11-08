"""Helpers for policy document downloads."""
import os
import re


def _safe(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "x"


def document_filename(policy):
    """<REGNO>_<policy_number>_<start YYYY-MM-DD>.<original extension>"""
    ext = os.path.splitext(policy.document.name)[1].lower() or ".bin"
    return "{}_{}_{}{}".format(
        _safe(policy.vehicle.registration_no), _safe(policy.policy_number), policy.start_date.isoformat(), ext)


def document_available(policy):
    """True if the policy has a document and the file really exists in storage."""
    f = policy.document
    return bool(f and f.name and f.storage.exists(f.name))

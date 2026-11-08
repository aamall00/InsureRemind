"""AI extraction of fields from an Indian motor insurance policy schedule."""
import base64
import json
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from .utils import normalize_reg_no

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Extraction failed; message is safe to show to the user."""


class ExtractionUnavailable(ExtractionError):
    """No API key configured."""


def _s():
    # Plain string ("" = not present): Anthropic structured outputs allow at most 16 union-typed params.
    return {"type": "string"}


STRING_FIELDS = [
    "owner_name", "registration_no", "make", "model", "variant", "fuel_type", "chassis_no",
    "engine_no", "insurer", "policy_number", "policy_type", "start_date", "expiry_date",
    "rto_location", "registration_date", "vehicle_usage",
    "previous_policy_number", "previous_insurer", "previous_policy_type",
    "previous_policy_start", "previous_policy_end",
]
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **{f: _s() for f in STRING_FIELDS},
        "manufacturing_year": {"type": ["integer", "null"]},
        "premium_total": {"type": ["number", "null"]},
        "net_premium": {"type": ["number", "null"]},
        "gst_amount": {"type": ["number", "null"]},
        "ncb_percent": {"type": ["number", "null"]},
        "previous_ncb_percent": {"type": ["number", "null"]},
        "previous_claims_count": {"type": ["integer", "null"]},
    },
}
SCHEMA["required"] = list(SCHEMA["properties"].keys())

PROMPT = """This is an Indian motor insurance policy document (cover letter / policy schedule). It may contain several sections, including "Insured & Vehicle Details", "Previous Policy Details", a premium table and terms.
Extract the fields defined in the JSON schema.
Rules:
- If a field is not present in the document, use an empty string "" for text/date fields and null for numeric fields. Never guess or infer a value.
- start_date and expiry_date are the CURRENT policy's Period of Insurance ONLY. Any dates, policy number, insurer, type, NCB or claims found under a "Previous Policy" section must go ONLY into the previous_* fields and must NEVER be placed in the current policy fields. previous_policy_start / previous_policy_end are the previous policy's period.
- Dates may appear in mixed formats (e.g. "Oct 17, 2026", "17-10-2025", "17/10/2025"). Indian numeric dates are DD-MM-YYYY. Always output dates as YYYY-MM-DD.
- Premiums are plain numbers without commas or currency symbols. premium_total is the "Total Premium Payable" (including GST). net_premium is the premium before taxes (e.g. "Total Package Premium(A+B)", "Net Premium", "Total Premium (before tax)"). gst_amount is the total tax payable (CGST+SGST+IGST combined).
- Identifiers (chassis_no, engine_no, registration_no, policy numbers) may be wrapped across several lines: join the pieces and remove all spaces.
- If make and model are given combined (e.g. "M & M / BOLERO B6"), put the manufacturer in make and the rest in model.
- The insured name goes in owner_name. Do NOT extract phone numbers or email addresses (they are masked in the document).
- rto_location is the RTO / city text as printed; vehicle_usage is the usage text as printed (e.g. "STAFF TRANSPORT").
- ncb_percent is the current year NCB and previous_ncb_percent the previous year NCB, as a number (20% becomes 20).
- previous_claims_count is the number of claims made under the previous policy (integer).
- registration_no is the vehicle registration number as printed; manufacturing_year is a 4-digit year.
- policy_type: describe the cover type as printed (e.g. Comprehensive / Package, Liability only / Third party, Own damage)."""

IMAGE_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
}


def _file_part(name, data):
    """Return (content part, is_pdf) for the OpenRouter chat message."""
    b64 = base64.standard_b64encode(data).decode()
    lower = name.lower()
    if lower.endswith(".pdf"):
        return {"type": "file", "file": {"filename": name, "file_data": f"data:application/pdf;base64,{b64}"}}, True
    for ext, mt in IMAGE_TYPES.items():
        if lower.endswith(ext):
            return {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{b64}"}}, False
    raise ExtractionError("Unsupported file type for extraction.")


def build_request(name, file_bytes):
    """Return (url, headers, payload) for the OpenRouter chat completions call."""
    cfg = settings.LLM
    part, is_pdf = _file_part(name, file_bytes)
    headers = {"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    if cfg.get("app_title"):
        headers["X-Title"] = cfg["app_title"]
    if cfg.get("app_url"):
        headers["HTTP-Referer"] = cfg["app_url"]
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": [part, {"type": "text", "text": PROMPT}]}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "policy_extraction", "strict": True, "schema": SCHEMA}},
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
    }
    if cfg.get("fallback_models"):
        payload["models"] = [cfg["model"], *cfg["fallback_models"]]
    if is_pdf:
        payload["plugins"] = [{"id": "file-parser", "pdf": {"engine": cfg["pdf_engine"]}}]
    return cfg["base_url"].rstrip("/") + "/chat/completions", headers, payload


def _strip_fence(text):
    t = text.strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", t, re.DOTALL)
    return m.group(1).strip() if m else t


STATUS_MESSAGES = {
    401: "The AI service rejected the API key. Check OPENROUTER_API_KEY, or enter details manually.",
    403: "The AI service rejected the API key. Check OPENROUTER_API_KEY, or enter details manually.",
    402: "OpenRouter credits are exhausted. Top up your balance, or enter details manually.",
    429: "The AI service is rate-limited right now. Please try again in a minute or enter details manually.",
}


def extract_policy_data(name, file_bytes):
    """Return the raw dict from the model. Raises ExtractionUnavailable / ExtractionError."""
    if not settings.OPENROUTER_API_KEY:
        raise ExtractionUnavailable("AI extraction is not configured (OPENROUTER_API_KEY is missing).")
    url, headers, payload = build_request(name, file_bytes)
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=settings.LLM["timeout_seconds"])
    except (requests.Timeout, requests.ConnectionError):
        raise ExtractionError("Could not reach the AI service (timeout or connection problem). Retry or enter details manually.")
    except requests.RequestException:
        raise ExtractionError("Could not reach the AI service. Retry or enter details manually.")

    status = resp.status_code
    if status >= 400 or status < 200:
        logger.warning("OpenRouter HTTP %s: %s", status, (resp.text or "")[:500])
        raise ExtractionError(STATUS_MESSAGES.get(
            status, f"The AI service returned an error (HTTP {status}). Enter details manually or retry later."))
    try:
        body = resp.json()
        if not isinstance(body, dict) or body.get("error") or not body.get("choices"):
            raise ValueError("no choices")
        content = body["choices"][0]["message"]["content"]
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        data = json.loads(_strip_fence(content))
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        logger.warning("OpenRouter unusable response: %s", (resp.text or "")[:500])
        raise ExtractionError("The AI service returned an unusable response. Please enter details manually.")
    data["_model"] = body.get("model", "")
    return data


# --- defensive parsing -------------------------------------------------------

def parse_date(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def parse_decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(re.sub(r"[,\s₹]|Rs\.?", "", str(value)))
    except (InvalidOperation, ValueError):
        return None
    return d.quantize(Decimal("0.01")) if d.is_finite() and d >= 0 else None


def parse_year(value):
    try:
        y = int(value)
    except (TypeError, ValueError):
        return None
    return y if 1950 <= y <= date.today().year + 1 else None


def map_policy_type(value):
    v = (value or "").lower()
    if not v:
        return ""
    if "third" in v or "liability" in v or "tp" == v.strip():
        return "third_party"
    if "own" in v and "damage" in v:
        return "own_damage"
    if "comprehensive" in v or "package" in v or "bundled" in v:
        return "comprehensive"
    return "other"


def _squash(value):
    return re.sub(r"\s+", "", value or "").upper()


def parse_percent(value):
    if value is None or isinstance(value, bool):
        return None
    d = parse_decimal(str(value).replace("%", ""))
    return d if d is not None and d <= 100 else None


def parse_count(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 1000 else None


def split_make_model(make, model):
    """'M & M / BOLERO B6' given in make with no model -> ('M & M', 'BOLERO B6')."""
    if "/" in make and not model:
        m, _, mo = make.partition("/")
        return m.strip(), mo.strip()
    return make, model


def clean_extracted(data):
    """Turn raw model output into values suitable for form initial data.

    The result includes a 'warnings' list of non-blocking suspicious-data notes.
    """
    data = {k: v for k, v in (data or {}).items() if k != "_model"}

    def st(k):
        v = data.get(k)
        return v.strip() if isinstance(v, str) else ""

    make, model = split_make_model(st("make"), st("model"))
    net, total = parse_decimal(data.get("net_premium")), parse_decimal(data.get("premium_total"))
    warnings = []
    if net is not None and total is not None and net > total:
        warnings.append("Net premium is greater than the total premium payable; the values may be swapped. Please check.")
    return {
        "owner_name": st("owner_name"),
        "registration_no": normalize_reg_no(st("registration_no")),
        "make": make, "model": model, "variant": st("variant"),
        "fuel_type": st("fuel_type"),
        "manufacturing_year": parse_year(data.get("manufacturing_year")),
        "chassis_no": _squash(st("chassis_no")), "engine_no": _squash(st("engine_no")),
        "rto_location": st("rto_location"),
        "registration_date": parse_date(data.get("registration_date")),
        "vehicle_usage": st("vehicle_usage"),
        "insurer": st("insurer"), "policy_number": st("policy_number"),
        "policy_type": map_policy_type(st("policy_type")),
        "premium": total, "net_premium": net, "gst_amount": parse_decimal(data.get("gst_amount")),
        "ncb_percent": parse_percent(data.get("ncb_percent")),
        "start_date": parse_date(data.get("start_date")), "expiry_date": parse_date(data.get("expiry_date")),
        "previous_policy_number": st("previous_policy_number"),
        "previous_insurer": st("previous_insurer"),
        "previous_policy_type": st("previous_policy_type"),
        "previous_policy_start": parse_date(data.get("previous_policy_start")),
        "previous_policy_end": parse_date(data.get("previous_policy_end")),
        "previous_ncb_percent": parse_percent(data.get("previous_ncb_percent")),
        "previous_claims_count": parse_count(data.get("previous_claims_count")),
        "warnings": warnings,
    }

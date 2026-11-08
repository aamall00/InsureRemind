import re

from django.core.exceptions import ValidationError

DEFAULT_COUNTRY_CODE = "91"


def normalize_reg_no(value):
    """Uppercase and strip spaces, hyphens and dots: 'mh 12 ab-1234' -> 'MH12AB1234'."""
    if not value:
        return ""
    return re.sub(r"[\s\-.]", "", str(value)).upper()


def normalize_phone(value, default_country=DEFAULT_COUNTRY_CODE):
    """Return an E.164 number. Raises ValidationError if it cannot be interpreted.

    '98765 43210', '09876543210', '+91-98765-43210', '919876543210' -> '+919876543210'
    """
    raw = (value or "").strip()
    if not raw:
        raise ValidationError("Phone number is required.")
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if has_plus or digits.startswith("00"):
        if digits.startswith("00"):
            digits = digits[2:]
    elif len(digits) == 10:
        digits = default_country + digits
    elif len(digits) == 11 and digits.startswith("0"):
        digits = default_country + digits[1:]
    elif len(digits) == 12 and digits.startswith(default_country):
        pass
    else:
        raise ValidationError("Enter a valid phone number (10-digit Indian mobile or +country code).")
    if digits.startswith(default_country) and len(digits) != len(default_country) + 10:
        raise ValidationError("Indian phone numbers must have 10 digits after +91.")
    if not 8 <= len(digits) <= 15:
        raise ValidationError("Enter a valid phone number.")
    return "+" + digits

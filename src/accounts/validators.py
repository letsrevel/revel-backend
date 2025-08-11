import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

PHONE_REGEX = re.compile(r"^\+?\d{7,15}$")  # E.164-ish: +[country][number], up to 15 digits


def validate_phone_number(value: str | None) -> None:
    """Validate phone number.

    Args:
        value (str): phone number.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(_("Phone number must be a string."))

    normalized = normalize_phone_number(value)

    if not PHONE_REGEX.fullmatch(normalized):
        raise ValidationError(_("Number format is incorrect."))
    return None


def normalize_phone_number(value: str) -> str:
    """Normalize phone number.

    Args:
        value (str): phone number.

    Returns:
        str: normalized phone number.
    """
    return re.sub(r"[ \-()]", "", value)

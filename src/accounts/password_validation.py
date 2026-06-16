"""Custom Django password validators."""

import re

from django.contrib.auth.password_validation import validate_password as _default_validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from ninja.errors import HttpError

from accounts.models import RevelUser


def validate_password(password: str, user: RevelUser | None = None) -> None:
    """Simple wrapper around Django's password validation."""
    try:
        _default_validate_password(password, user=user)
    except ValidationError as e:
        raise HttpError(400, e.messages[0])


class ComplexPasswordValidator:
    """Custom Django password validator enforcing complexity rules."""

    def __init__(self, min_length: int = 8) -> None:
        """Initializes the password validator."""
        self.min_length = min_length

    def validate(self, password: str, user: RevelUser | None = None) -> None:
        """Validates the password against custom rules."""
        if len(password) < self.min_length:
            raise ValidationError(
                _("Password must be at least %(min_length)d characters long.") % {"min_length": self.min_length},
                code="password_too_short",
            )

        if not re.search(r"[A-Z]", password):
            raise ValidationError(
                _("Password must contain at least one uppercase letter."),
                code="password_no_upper",
            )

        if not re.search(r"[a-z]", password):
            raise ValidationError(
                _("Password must contain at least one lowercase letter."),
                code="password_no_lower",
            )

        if not re.search(r"\d", password):
            raise ValidationError(
                _("Password must contain at least one digit."),
                code="password_no_digit",
            )

        if not re.search(r"[!@#$%^&*(),.?\":{}|<>-\[\]=]", password):
            raise ValidationError(
                _('Password must contain at least one special character (!@#$%^&*(),.?":{}|<>)[]=.'),
                code="password_no_special",
            )

    def get_help_text(self) -> str:
        """Returns the help text for the password validator."""
        # NB: %% escapes the literal percent in the special-character list so the
        # %(min_length)d interpolation below doesn't choke on it.
        return _(
            "Your password must be at least %(min_length)d characters long, "
            "contain at least one uppercase letter, one lowercase letter, one digit, "
            'and one special character (!@#$%%^&*(),.?":{}|<>).'
        ) % {"min_length": self.min_length}

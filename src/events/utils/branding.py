"""Organization branding blob (#1149).

``Organization.branding`` is a JSON field validated by :class:`OrganizationBranding`.

The rule for what belongs in it: the blob holds **only org-owned presentation choices**
that an organization admin may freely change. Every new key must have a default, so
existing rows stay valid without a data migration. Files (logo, cover art), ``custom_domain``
(unique, looked up per request) and platform-admin flags (``custom_domain_enabled``, any
future "hide Powered by Revel") never go here: they stay real columns so org admins cannot
grant themselves platform-level switches through the branding endpoint.
"""

import typing as t

import structlog
from django.utils.translation import gettext_lazy as _
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError
from pydantic_core import PydanticCustomError

from events.utils.brand_theme import is_valid_hex_color

logger = structlog.get_logger(__name__)


def _validate_hex_color(value: str) -> str:
    """Normalize a ``#RRGGBB`` color to uppercase, or raise a translatable error."""
    if not is_valid_hex_color(value):
        raise PydanticCustomError(
            "invalid_hex_color", str(_("Enter a color as a 6-digit hex value, for example #8C3CDD."))
        )
    return value.upper()


HexColor = t.Annotated[str, AfterValidator(_validate_hex_color)]
"""A ``#RRGGBB`` color (either case in, uppercase out)."""


class OrganizationBranding(BaseModel):
    """An organization's presentation settings, such as its brand colors.

    Colors are ``#RRGGBB`` (stored uppercase); ``null`` means the platform default.
    """

    # Boundary rule (see module docstring): org-owned presentation choices only, every new key
    # needs a default; files, custom_domain and platform-admin flags stay real model columns.

    model_config = ConfigDict(extra="forbid", frozen=True)

    primary_color: HexColor | None = None
    accent_color: HexColor | None = None


def parse_branding(raw: t.Any) -> OrganizationBranding:
    """Parse a stored branding blob, tolerating bad data.

    Reads must never fail: if the stored value does not validate (for example it was
    written around ``clean()`` with ``QuerySet.update``), log a warning and return empty
    branding.

    Args:
        raw: The value of ``Organization.branding``.

    Returns:
        The validated branding, or an empty :class:`OrganizationBranding`.
    """
    try:
        return OrganizationBranding.model_validate(raw or {})
    except ValidationError:
        logger.warning("invalid_organization_branding_blob")
        return OrganizationBranding()

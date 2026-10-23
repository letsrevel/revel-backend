"""Organization brand colors for wallet passes and pass/ticket PDFs (#1157).

One pure mapping from an organization to the colors every pass surface needs, so Apple
Wallet, Google Wallet and the PDFs cannot drift apart. ``None`` means "not branded": callers
then keep the Revel defaults untouched.

Wallet cards and the PDF banner are a solid *surface*, not text on the page background, so
the page-contrast shifting :func:`~events.utils.brand_theme.derive_brand_theme` applies for
the web UI would only make the brand color less recognizable (a light pink turning brown).
The organization's own primary is therefore used as is for the surface, with black or white
on top (whichever contrasts more, always at least 4.5:1). Where the brand color appears as
small text or thin rules on the white/lavender PDF paper, the derived light-mode colors are
used instead, because those are guaranteed legible against that surface.
"""

import functools
import typing as t

from pydantic import BaseModel, ConfigDict

from events.utils.brand_theme import derive_brand_theme, foreground_for, hex_to_rgb
from events.utils.branding import parse_branding

if t.TYPE_CHECKING:
    from events.models import Organization


class PassBrandColors(BaseModel):
    """Colors for one branded pass, all uppercase ``#RRGGBB``."""

    model_config = ConfigDict(frozen=True)

    background: str
    """Solid surface (wallet card, PDF banner): the organization's primary color."""
    foreground: str
    """Black or white, whichever reads best on ``background`` (>= 4.5:1)."""
    text_accent: str
    """Primary-derived color for small text and rules on the white/lavender PDF paper (>= 4.5:1)."""


@functools.lru_cache(maxsize=1024)
def _pass_brand_colors(primary: str | None) -> PassBrandColors | None:
    theme = derive_brand_theme(primary, None)
    if theme is None or primary is None:
        return None
    return PassBrandColors(
        background=primary,
        foreground=foreground_for(hex_to_rgb(primary)),
        text_accent=theme.light.primary,
    )


def get_pass_brand_colors(organization: "Organization") -> PassBrandColors | None:
    """Return the pass colors for an organization, or ``None`` when it has no brand color.

    Reads ``organization.branding`` (already loaded with the row, no extra query) through the
    tolerant :func:`~events.utils.branding.parse_branding`, so invalid stored data falls
    back to the Revel defaults instead of failing pass generation.

    Args:
        organization: The organization that issues the pass.

    Returns:
        The pass colors, or ``None`` when no primary color is set.
    """
    branding = parse_branding(organization.branding)
    return _pass_brand_colors(branding.primary_color)

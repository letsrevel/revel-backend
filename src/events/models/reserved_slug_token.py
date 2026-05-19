"""ReservedSlugToken — soft, admin-editable additions to the reserved-token list."""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel


class ReservedSlugToken(TimeStampedModel):
    """A single token that blocks Organization creation when present in the slugified name.

    Tokens are stored lowercased and pre-slugified. A token must be exactly one
    slug segment (no dashes, no whitespace, ASCII). Hardcoded baseline tokens
    live in ``events.constants.reserved_slug_tokens``; this table is unioned
    with that set at lookup time.
    """

    token = models.CharField(max_length=64, unique=True)
    reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["token"]
        verbose_name = "Reserved slug token"
        verbose_name_plural = "Reserved slug tokens"

    def __str__(self) -> str:
        return self.token

    def clean(self) -> None:
        """Normalize the token to a single lowercase slug segment."""
        super().clean()
        normalized = slugify(self.token or "")
        if not normalized or "-" in normalized:
            raise DjangoValidationError({"token": _("Token must be a single non-empty slug segment.")})
        self.token = normalized

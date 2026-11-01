"""Exceptions shared across apps that render with a machine-readable ``code``."""

import enum
import typing as t

from django.utils.translation import gettext_lazy as _

if t.TYPE_CHECKING:
    from django_stubs_ext import StrPromise


class VIESUnavailableCode(str, enum.Enum):
    """Stable discriminators for the VAT-ID 503s raised while VIES is down.

    All share one status, but the frontend must tell them apart:

    - ``vies_unavailable_saved_pending``: the VAT ID was saved unvalidated and a background
      revalidation is queued (organizations).
    - ``vies_unavailable_saved_unvalidated``: the VAT ID was saved unvalidated and NO retry is
      queued; the client must retry later (personal billing profiles).
    - ``vies_unavailable_not_saved``: the change was refused and nothing was written.

    Renaming a value is a breaking change; rewording the message is always safe.
    """

    SAVED_PENDING = "vies_unavailable_saved_pending"
    SAVED_UNVALIDATED = "vies_unavailable_saved_unvalidated"
    NOT_SAVED = "vies_unavailable_not_saved"


class VIESUnavailableResponseError(Exception):
    """Base for VIES-outage refusals rendered with a ``code``; subclasses are bare-raised."""

    code: t.ClassVar[VIESUnavailableCode]
    message: t.ClassVar["StrPromise"]


class VATIdSavedPendingError(VIESUnavailableResponseError):
    """VIES is down: the VAT ID was saved unvalidated and a background revalidation is queued."""

    code = VIESUnavailableCode.SAVED_PENDING
    message = _(
        "VIES validation service is temporarily unavailable. The VAT ID has been saved. Please try again later."
    )


class VATIdNotSavedError(VIESUnavailableResponseError):
    """VIES is down and the outcome decides the change: nothing was written."""

    code = VIESUnavailableCode.NOT_SAVED
    message = _("VIES validation service is temporarily unavailable. Please try again later.")


class VATIdSavedUnvalidatedError(VIESUnavailableResponseError):
    """VIES is down: the VAT ID was saved unvalidated and nothing retries it; the client should retry later."""

    code = VIESUnavailableCode.SAVED_UNVALIDATED
    message = VATIdSavedPendingError.message  # same msgid: no catalog change

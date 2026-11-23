"""The outbound email backend: a guard that never sends to the placeholder address of an erased account.

An erased account keeps its row with the address ``erased-<id>@erased.invalid`` (see
``accounts.service.account_erasure``). Every outbound email goes through a Django email backend, so this
backend is the one place that sees all of them: notifications, transactional mail, invoices and
org-branded mail. It removes each such recipient (To, Cc and Bcc), logs the drop without the address,
and sends the message only when a recipient is left. The real transport is ``EMAIL_GUARDED_BACKEND``.
"""

import re
import typing as t
from collections.abc import Sequence

import structlog
from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMessage

logger = structlog.get_logger(__name__)

ERASED_EMAIL_DOMAIN = "erased.invalid"
# ``common.tasks.to_safe_email_address`` rewrites "@" and "." when live emails are off, so the placeholder can
# also arrive as "<catch-all user>+erased-<id>_at_erased_dot_invalid@<catch-all domain>". Only that exact form
# at the configured catch-all address counts: a real mailbox whose name has the same ending gets its mail.
_REWRITTEN_ERASED_SUFFIX = "_at_erased_dot_invalid"
_REWRITTEN_PLACEHOLDER = re.compile(r"erased-[0-9a-f]{32}" + _REWRITTEN_ERASED_SUFFIX)


def erased_placeholder_email(user_id_hex: str) -> str:
    """The unique placeholder address (and username) of an erased account."""
    return f"erased-{user_id_hex}@{ERASED_EMAIL_DOMAIN}"


def is_erased_address(address: str) -> bool:
    """Whether ``address`` (bare, or with a display name) is the placeholder of an erased account.

    Every address at the placeholder domain counts (the ``.invalid`` domain has no mailbox). The rewritten
    form counts only when it is the generated placeholder at the configured catch-all address.
    """
    bare = address.strip().rstrip(">").rpartition("<")[2].strip().lower()
    local, _, domain = bare.rpartition("@")
    if domain == ERASED_EMAIL_DOMAIN:  # "a@erased.invalid.org" is a real address
        return True
    return local.endswith(_REWRITTEN_ERASED_SUFFIX) and _is_rewritten_placeholder(local, domain)


def _is_rewritten_placeholder(local: str, domain: str) -> bool:
    """Whether ``local@domain`` is what the catch-all rewrite makes of a placeholder address."""
    from common.models import SiteSettings  # lazy: this module is loaded as the email backend

    catchall_user, _, catchall_domain = SiteSettings.get_solo().internal_catchall_email.lower().rpartition("@")
    prefix = f"{catchall_user}+"
    return (
        domain == catchall_domain
        and local.startswith(prefix)
        and _REWRITTEN_PLACEHOLDER.fullmatch(local[len(prefix) :]) is not None
    )


class ErasedRecipientGuardBackend(BaseEmailBackend):
    """Drop erased-account recipients, then hand the message to ``settings.EMAIL_GUARDED_BACKEND``."""

    def __init__(self, fail_silently: bool = False, **kwargs: t.Any) -> None:
        """Build the inner transport with the same options (host, port, credentials...)."""
        super().__init__(fail_silently=fail_silently)
        self._inner = get_connection(settings.EMAIL_GUARDED_BACKEND, fail_silently=fail_silently, **kwargs)

    def open(self) -> bool | None:
        """Open the inner transport's connection."""
        return self._inner.open()

    def close(self) -> None:
        """Close the inner transport's connection."""
        self._inner.close()

    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        """Send the messages that still have a recipient after the guard; return the inner backend's count."""
        to_send = [message for message in email_messages if _drop_erased_recipients(message)]
        if not to_send:
            return 0
        return int(self._inner.send_messages(to_send) or 0)


def _drop_erased_recipients(message: EmailMessage) -> bool:
    """Remove erased-account recipients from ``message`` in place. Return whether a recipient is left."""
    dropped = 0
    for attr in ("to", "cc", "bcc"):
        addresses: list[str] = list(getattr(message, attr) or [])
        kept = [address for address in addresses if not is_erased_address(address)]
        dropped += len(addresses) - len(kept)
        setattr(message, attr, kept)
    if dropped:
        # No address in the log: the placeholder names the account, and the subject is enough to trace it.
        logger.warning("email_erased_recipient_dropped", dropped=dropped, subject=str(message.subject)[:120])
    if not message.recipients():
        logger.warning("email_dropped_no_recipient_left", subject=str(message.subject)[:120])
        return False
    return True

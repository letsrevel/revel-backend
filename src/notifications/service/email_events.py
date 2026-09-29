"""Provider email events (bounces, complaints) → suppression list.

Narrow by design: a provider adapter (:func:`parse_brevo`) turns a webhook payload into
provider-neutral :class:`ProviderEmailEvent` objects, and :func:`record_email_event`
applies them. Adding another ESP means adding another ``parse_*`` function only.

Clearing a suppression is an admin delete. Changing a user's email does not clear the old
address's row: the user simply stops matching it.
"""

import base64
import binascii
import hmac
import json
import typing as t
from dataclasses import dataclass
from uuid import UUID

import structlog
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from events.models import Organization
from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import EmailSuppression, NotificationDelivery
from notifications.service.email_policy import suppress

logger = structlog.get_logger(__name__)

EmailEventKind = t.Literal["hard_bounce", "complaint", "blocked", "invalid", "ignored"]

# Transactional webhooks send snake_case, marketing webhooks camelCase. Everything else
# (soft_bounce, deferred, error, unsubscribed, delivered, opened, …) is ignored.
_BREVO_KINDS: dict[str, EmailEventKind] = {
    "hard_bounce": "hard_bounce",
    "hardBounce": "hard_bounce",
    "spam": "complaint",
    "blocked": "blocked",
    "invalid_email": "invalid",
    "invalid": "invalid",
}

_SUPPRESSION_REASONS: dict[EmailEventKind, EmailSuppression.Reason] = {
    "hard_bounce": EmailSuppression.Reason.HARD_BOUNCE,
    "complaint": EmailSuppression.Reason.COMPLAINT,
    "blocked": EmailSuppression.Reason.BLOCKED,
    "invalid": EmailSuppression.Reason.INVALID,
}

# Keys of the ``X-Mailin-custom`` correlation header written at send time.
_CORRELATION_KEYS = {"delivery": "delivery_id", "org": "organization_id", "invitation": "invitation_id"}


@dataclass(frozen=True)
class ProviderEmailEvent:
    """A provider-neutral email event."""

    email: str
    kind: EmailEventKind
    delivery_id: UUID | None
    organization_id: UUID | None
    invitation_id: UUID | None
    detail: str


def is_authorized(authorization: str) -> bool:
    """Check a webhook ``Authorization`` header against ``EMAIL_WEBHOOK_SECRET``.

    Accepts ``Bearer <secret>`` or ``Basic`` credentials whose password is the secret
    (Brevo's UI only supports basic auth embedded in the webhook URL).

    Args:
        authorization: Raw ``Authorization`` header value (empty if absent).

    Returns:
        True if the header carries the configured secret. Always False when unset.
    """
    secret: str = settings.EMAIL_WEBHOOK_SECRET
    if not secret:
        return False
    scheme, _, credentials = authorization.partition(" ")
    scheme = scheme.lower()
    if scheme == "bearer":
        candidate = credentials
    elif scheme == "basic":
        try:
            decoded = base64.b64decode(credentials, validate=True).decode()
        except binascii.Error, UnicodeDecodeError:
            return False
        _user, sep, candidate = decoded.partition(":")
        if not sep:
            return False
    else:
        return False
    return hmac.compare_digest(candidate.encode(), secret.encode())


def load_json_body(body: bytes) -> dict[str, t.Any] | list[t.Any]:
    """Decode a webhook body that must be a JSON object or array.

    Raises:
        ValidationError: If the body isn't a JSON object or array (mapped to 400).
    """
    try:
        payload = json.loads(body)
    except ValueError:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        payload = None
    if not isinstance(payload, dict | list):
        raise ValidationError("Webhook body must be a JSON object or array.")
    return payload


def _parse_custom_header(value: t.Any) -> dict[str, UUID]:
    """Parse ``key:uuid|key:uuid`` from ``X-Mailin-custom``, dropping anything garbled."""
    if not isinstance(value, str):
        return {}
    parsed: dict[str, UUID] = {}
    for part in value.split("|"):
        key, _, raw = part.partition(":")
        field = _CORRELATION_KEYS.get(key.strip())
        if field is None:
            continue
        try:
            parsed[field] = UUID(raw.strip())
        except ValueError:
            continue
    return parsed


def parse_brevo(payload: dict[str, t.Any] | list[t.Any]) -> list[ProviderEmailEvent]:
    """Translate a Brevo webhook payload (one event or a batch) into provider-neutral events.

    Items that aren't objects or carry no email are skipped.

    Args:
        payload: Decoded JSON body.

    Returns:
        One event per usable item; unknown event names have kind ``"ignored"``.
    """
    items = payload if isinstance(payload, list) else [payload]
    events: list[ProviderEmailEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        email = item.get("email")
        if not isinstance(email, str) or not email:
            continue
        correlation = _parse_custom_header(item.get("X-Mailin-custom"))
        events.append(
            ProviderEmailEvent(
                email=email,
                kind=_BREVO_KINDS.get(str(item.get("event", "")), "ignored"),
                delivery_id=correlation.get("delivery_id"),
                organization_id=correlation.get("organization_id"),
                invitation_id=correlation.get("invitation_id"),
                detail=str(item.get("reason") or ""),
            )
        )
    return events


def _is_valid_address(email: str) -> bool:
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


def _mark_delivery(delivery_id: UUID, event: ProviderEmailEvent, reason: EmailSuppression.Reason) -> None:
    """Reflect the event on the correlated email delivery, if any (idempotent)."""
    with transaction.atomic():
        delivery = (
            NotificationDelivery.objects.select_for_update()
            .filter(id=delivery_id, channel=DeliveryChannel.EMAIL)
            .first()
        )
        if delivery is None:
            return
        if reason == EmailSuppression.Reason.COMPLAINT:
            # The mail was delivered; the recipient reported it. Status stays SENT.
            delivery.metadata = {**delivery.metadata, "complained": True}
            delivery.save(update_fields=["metadata", "updated_at"])
            return
        delivery.status = DeliveryStatus.FAILED
        delivery.metadata = {**delivery.metadata, "suppression_reason": reason}
        delivery.error_message = f"{reason}: {event.detail}" if event.detail else str(reason)
        delivery.save(update_fields=["status", "metadata", "error_message", "updated_at"])


def record_email_event(event: ProviderEmailEvent) -> None:
    """Apply a provider event: suppress the address and update the correlated delivery.

    Hard bounces, invalid and blocked addresses mark the delivery FAILED; complaints keep
    it SENT with ``metadata["complained"]``. Replays are no-ops (rank-aware suppression,
    same delivery values).

    Args:
        event: The provider-neutral event.
    """
    reason = _SUPPRESSION_REASONS.get(event.kind)
    if reason is None:
        return
    logger.info(
        "email_event_recorded",
        kind=event.kind,
        delivery_id=str(event.delivery_id) if event.delivery_id else None,
        invitation_id=str(event.invitation_id) if event.invitation_id else None,
        organization_id=str(event.organization_id) if event.organization_id else None,
    )
    if _is_valid_address(event.email):
        # The org may have been deleted since the send; drop attribution instead of failing the FK.
        org_id = event.organization_id
        if org_id and not Organization.objects.filter(id=org_id).exists():
            org_id = None
        suppress(event.email, reason, EmailSuppression.Source.PROVIDER, organization_id=org_id, detail=event.detail)
    else:
        logger.warning("email_event_unstorable_address", kind=event.kind)
    if event.delivery_id:
        _mark_delivery(event.delivery_id, event, reason)

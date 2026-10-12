"""Who Revel may email: user preferences and the address-level suppression list."""

from collections.abc import Iterable
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from accounts.models import RevelUser
from accounts.utils.email_normalization import normalize_email_for_matching
from common.utils import get_or_create_with_race_protection
from notifications.enums import DeliveryChannel
from notifications.models import EmailSuppression


def may_email(user: RevelUser, notification_type: str) -> bool:
    """Whether the user's preferences allow emailing them this notification type.

    Preferences only — no DB access beyond ``user.notification_preferences`` and no
    suppression lookup (see :func:`suppression_for`).

    Args:
        user: Recipient.
        notification_type: Notification type value.

    Returns:
        True if the user has an address and EMAIL is an effective channel for the type.
    """
    if not user.email:
        return False
    channels = user.notification_preferences.get_channels_for_notification_type(notification_type)
    return DeliveryChannel.EMAIL in channels


def suppression_for(email: str, *, include_opt_out: bool = False) -> EmailSuppression | None:
    """Return the suppression row blocking ``email``, if any.

    ``INVITATION_OPT_OUT`` rows only count when ``include_opt_out`` is True (cold mail
    to non-users). Mail to users ignores them, so a person who declined an org's cold
    invite and later registers still gets their tickets.

    Args:
        email: Raw address; normalized before lookup.
        include_opt_out: Whether invitation opt-outs block delivery.

    Returns:
        The blocking suppression, or None.
    """
    qs = EmailSuppression.objects.filter(email=normalize_email_for_matching(email))
    if not include_opt_out:
        qs = qs.exclude(reason=EmailSuppression.Reason.INVITATION_OPT_OUT)
    return qs.first()


def suppressed_addresses(emails: Iterable[str]) -> set[str]:
    """Batch form of :func:`suppression_for` for mail to users (opt-outs ignored).

    Args:
        emails: Raw addresses; normalized before lookup.

    Returns:
        The normalized addresses among ``emails`` that are suppressed.
    """
    normalized = {normalize_email_for_matching(email) for email in emails if email}
    return set(
        EmailSuppression.objects.filter(email__in=normalized)
        .exclude(reason=EmailSuppression.Reason.INVITATION_OPT_OUT)
        .values_list("email", flat=True)
    )


def suppress(
    email: str,
    reason: EmailSuppression.Reason,
    source: EmailSuppression.Source,
    organization_id: UUID | None = None,
    detail: str = "",
) -> EmailSuppression:
    """Record a suppression for ``email`` (rank-aware upsert).

    Rank: COMPLAINT > HARD_BOUNCE = INVALID = BLOCKED > INVITATION_OPT_OUT. A write of
    lower or equal rank never overwrites an existing row (so provider replays are
    no-ops); a higher rank overwrites reason, source, organization and detail.

    Args:
        email: Raw address; normalized before storing.
        reason: Why the address is suppressed.
        source: Who reported it.
        organization_id: Organization whose mail triggered it, if known.
        detail: Provider reason text.

    Returns:
        The stored (possibly pre-existing, unchanged) suppression row.
    """
    normalized = normalize_email_for_matching(email)
    fields = {"reason": reason, "source": source, "organization_id": organization_id, "detail": detail}
    suppression, created = get_or_create_with_race_protection(
        EmailSuppression, Q(email=normalized), {"email": normalized, **fields}
    )
    if created:
        return suppression
    rank = EmailSuppression.REASON_RANK
    with transaction.atomic():
        # Lock so two concurrent upgrades can't let the lower rank win last.
        suppression = EmailSuppression.objects.select_for_update().get(pk=suppression.pk)
        if rank[reason] <= rank[suppression.reason]:
            return suppression
        for field, value in fields.items():
            setattr(suppression, field, value)
        suppression.save(update_fields=[*fields, "updated_at"])
    return suppression

"""Pure query helpers for blacklist-related model managers.

This module is intentionally side-effect free and contains only query
helpers that are safely importable from model managers (which must not
depend on the service layer). See ``events/CLAUDE.md`` for the rule:
``controllers → services → models/utils``.
"""

import typing as t

from django.db.models import Q

if t.TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import QuerySet

    from accounts.models import RevelUser
    from events.models import Blacklist


def get_hard_blacklisted_org_ids(user: "RevelUser") -> "QuerySet[Blacklist, UUID]":
    """Get organization IDs where user is hard-blacklisted.

    Used by for_user() managers to exclude blacklisted organizations.

    Args:
        user: The user to check

    Returns:
        ValuesQuerySet of organization IDs
    """
    # Imported lazily to avoid circular imports at model-loading time.
    from events.models import Blacklist

    q = Q(user=user)

    if user.email:
        q |= Q(email__iexact=user.email)

    if user.phone_number:
        q |= Q(phone_number=user.phone_number)

    if telegram_usernames := list(user.telegram_users.values_list("telegram_username", flat=True)):
        q |= Q(telegram_username__in=[u.lower() for u in telegram_usernames if u])

    return Blacklist.objects.filter(q).values_list("organization_id", flat=True)


def get_hard_blacklisted_user_ids(organization_id: "UUID") -> "QuerySet[RevelUser, UUID]":
    """User IDs hard-blacklisted in an organization (bulk twin of ``check_user_hard_blacklisted``).

    Matches the same way: the entry's user FK, or its email (case-insensitive), phone number or telegram
    username against the account's. A blacklist never creates a membership row, so any code that must
    exclude banned people looks here, not at ``OrganizationMember``.

    Args:
        organization_id: The organization whose blacklist to read

    Returns:
        ValuesQuerySet of account IDs (narrow it with ``.filter(pk__in=...)`` for a candidate set)
    """
    from django.db.models import Exists, OuterRef
    from django.db.models.functions import Lower

    from accounts.models import RevelUser
    from events.models import Blacklist
    from telegram.models import TelegramUser

    entries = Blacklist.objects.filter(organization_id=organization_id)
    telegram = TelegramUser.objects.filter(user=OuterRef("pk"), telegram_username__isnull=False).annotate(
        handle=Lower("telegram_username")
    )
    return RevelUser.objects.filter(
        Q(pk__in=entries.filter(user__isnull=False).values("user_id"))
        | (~Q(email="") & Exists(entries.filter(email__iexact=OuterRef("email"))))
        | (
            Q(phone_number__isnull=False)
            & ~Q(phone_number="")
            & Exists(entries.filter(phone_number=OuterRef("phone_number")))
        )
        | Exists(telegram.filter(Exists(entries.filter(telegram_username=OuterRef("handle")))))
    ).values_list("pk", flat=True)

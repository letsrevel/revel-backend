"""Pure query helpers for blacklist-related model managers.

This module is intentionally side-effect free and contains only query
helpers that are safely importable from model managers (which must not
depend on the service layer). See ``events/CLAUDE.md`` for the rule:
``controllers → services → models/utils``.
"""

import typing as t

from django.db.models import Q

if t.TYPE_CHECKING:
    from django.db.models import QuerySet

    from accounts.models import RevelUser
    from events.models import Blacklist


def get_hard_blacklisted_org_ids(user: "RevelUser") -> "QuerySet[Blacklist]":
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

    return Blacklist.objects.filter(q).values_list("organization_id", flat=True)  # type: ignore[return-value]

"""Service functions for bookmarking events."""

from uuid import UUID

from django.db.models import Q

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from events.models import Event, EventBookmark


def bookmark_event(user: RevelUser, event: Event) -> EventBookmark:
    """Bookmark an event for a user (idempotent).

    Args:
        user: The user bookmarking the event.
        event: The event to bookmark. Callers must resolve this from a
            user-visible queryset (e.g. ``Event.objects.for_user``) so users
            can only bookmark events they are allowed to see.

    Returns:
        The existing or newly created bookmark.
    """
    bookmark, _ = get_or_create_with_race_protection(
        EventBookmark,
        Q(user=user, event=event),
        {"user": user, "event": event},
    )
    return bookmark


def unbookmark_event(user: RevelUser, event_id: UUID) -> None:
    """Remove a user's bookmark for an event (idempotent hard delete).

    Resolves by id without a visibility check so users can always remove their
    own bookmark — even for an event they have since lost access to. A no-op if
    no bookmark exists.

    Args:
        user: The user removing the bookmark.
        event_id: The id of the event to unbookmark.
    """
    EventBookmark.objects.filter(user=user, event_id=event_id).delete()

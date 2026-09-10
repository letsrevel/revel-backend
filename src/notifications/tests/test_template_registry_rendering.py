"""Registry-wide rendering coverage for notification templates.

Unlike ``test_email_templates_branding.py`` (which renders the Django HTML email
templates), this module exercises the *Python* template classes reached through the
real registry: every ``NotificationType`` must resolve to a template whose
``get_in_app_title`` / ``get_email_subject`` return a non-empty, fully-substituted
string.

It also pins the branchy title/subject logic of the potluck and waitlist templates,
and the straight-line follow / whitelist / invitation templates.
"""

import typing as t

import pytest

from accounts.models import RevelUser
from notifications.context_schemas import NOTIFICATION_CONTEXT_SCHEMAS
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template, is_template_registered

pytestmark = pytest.mark.django_db


# --- Helpers ---


def _sample_value(key: str, annotation: t.Any) -> t.Any:
    """Build a plausible sample value for one required context key."""
    origin = t.get_origin(annotation)
    if origin is list:
        args = t.get_args(annotation)
        return ["Sample item"] if args and args[0] is str else []
    if origin is dict:
        return {"field": "value"}
    if annotation is bool:
        return True
    if annotation is int:
        return 2
    return f"Sample {key.replace('_', ' ')}"


def _sample_context(notification_type: NotificationType) -> dict[str, t.Any]:
    """Build a context satisfying every required key of the type's schema."""
    schema = NOTIFICATION_CONTEXT_SCHEMAS[notification_type]
    annotations: dict[str, t.Any] = {}
    for klass in reversed(schema.__mro__):
        annotations.update(getattr(klass, "__annotations__", {}))
    required: set[str] = getattr(schema, "__required_keys__", set())
    return {key: _sample_value(key, annotations.get(key, str)) for key in required}


def _make_notification(
    user: RevelUser,
    notification_type: NotificationType,
    context: dict[str, t.Any],
) -> Notification:
    """Create a notification without going through context validation."""
    return Notification.objects.create(user=user, notification_type=notification_type, context=context)


def _assert_rendered(value: str) -> None:
    """A rendered title/subject must be a non-empty, fully-substituted string."""
    assert isinstance(value, str)
    assert value.strip()
    assert "%(" not in value
    assert "%s" not in value
    assert "%d" not in value


# --- Registry-wide coverage ---


@pytest.mark.parametrize("notification_type", list(NotificationType))
def test_every_notification_type_renders_title_and_subject(
    regular_user: RevelUser, notification_type: NotificationType
) -> None:
    """Every notification type resolves to a template that renders both headers."""
    assert is_template_registered(notification_type)
    template = get_template(notification_type)
    notification = _make_notification(regular_user, notification_type, _sample_context(notification_type))

    _assert_rendered(template.get_in_app_title(notification))
    _assert_rendered(template.get_email_subject(notification))


# --- Potluck templates ---


POTLUCK_ACTIONS = ["created", "created_and_claimed", "claimed", "unclaimed", "deleted", ""]


@pytest.mark.parametrize("action", POTLUCK_ACTIONS)
@pytest.mark.parametrize("is_organizer", [True, False])
@pytest.mark.parametrize("actor_name", ["Ada", None])
def test_potluck_title_and_subject_branches(
    regular_user: RevelUser, action: str, is_organizer: bool, actor_name: str | None
) -> None:
    """Every organizer/action combination yields a substituted title and subject."""
    notification = _make_notification(
        regular_user,
        NotificationType.POTLUCK_ITEM_CREATED,
        {
            "action": action,
            "item_name": "Potato Salad",
            "event_name": "Summer Picnic",
            "actor_name": actor_name,
            "is_organizer": is_organizer,
        },
    )
    template = get_template(NotificationType.POTLUCK_ITEM_CREATED)

    title = template.get_in_app_title(notification)
    subject = template.get_email_subject(notification)
    _assert_rendered(title)
    _assert_rendered(subject)
    assert "Potato Salad" in title
    if is_organizer and actor_name and action in ("created", "created_and_claimed", "claimed"):
        assert actor_name in title
        assert actor_name in subject
    else:
        assert "Summer Picnic" in subject


def test_potluck_unknown_action_falls_back_to_generic_title(regular_user: RevelUser) -> None:
    """An unrecognised action still names the item."""
    notification = _make_notification(
        regular_user,
        NotificationType.POTLUCK_ITEM_UPDATED,
        {"action": "wobbled", "item_name": "Cake", "event_name": "Party"},
    )
    template = get_template(NotificationType.POTLUCK_ITEM_UPDATED)

    assert template.get_in_app_title(notification) == "Potluck Update: Cake"
    assert template.get_email_subject(notification) == "Potluck Update - Party"


def test_potluck_deleted_subject_is_event_scoped(regular_user: RevelUser) -> None:
    """A deletion subject names the event, not the item."""
    notification = _make_notification(
        regular_user,
        NotificationType.POTLUCK_ITEM_DELETED,
        {"action": "deleted", "item_name": "Cake", "event_name": "Party", "is_organizer": True},
    )
    template = get_template(NotificationType.POTLUCK_ITEM_DELETED)

    assert template.get_in_app_title(notification) == "Potluck Item Removed: Cake"
    assert template.get_email_subject(notification) == "Potluck Item Removed - Party"


# --- Waitlist template ---


@pytest.mark.parametrize("spots", [None, 1, 3])
def test_waitlist_title_handles_offer_and_broadcast_contexts(regular_user: RevelUser, spots: int | None) -> None:
    """Per-user offers (no count) and broadcasts (a count) both render."""
    context: dict[str, t.Any] = {"event_name": "Sold Out Show", "event_id": "abc"}
    if spots is not None:
        context["spots_available"] = spots
    notification = _make_notification(regular_user, NotificationType.WAITLIST_SPOT_AVAILABLE, context)
    template = get_template(NotificationType.WAITLIST_SPOT_AVAILABLE)

    title = template.get_in_app_title(notification)
    _assert_rendered(title)
    assert "Sold Out Show" in title
    if spots is None or spots == 1:
        assert title == "Spot available for Sold Out Show!"
    else:
        assert title == "3 spots available for Sold Out Show!"

    assert template.get_email_subject(notification) == "Spot Available: Sold Out Show"


# --- Follow templates ---


@pytest.mark.parametrize(
    ("notification_type", "context", "expected_title", "expected_subject"),
    [
        (
            NotificationType.ORGANIZATION_FOLLOWED,
            {"follower_name": "Ada", "organization_name": "Revel"},
            "Ada started following Revel",
            "New follower - Revel",
        ),
        (
            NotificationType.EVENT_SERIES_FOLLOWED,
            {"follower_name": "Ada", "event_series_name": "Weekly"},
            "Ada started following Weekly",
            "New follower - Weekly",
        ),
        (
            NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG,
            {"organization_name": "Revel", "event_name": "Launch"},
            "Revel created Launch",
            "New event from Revel",
        ),
        (
            NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES,
            {"event_series_name": "Weekly", "event_name": "Launch"},
            "New event in Weekly: Launch",
            "New event in Weekly",
        ),
    ],
)
def test_follow_templates(
    regular_user: RevelUser,
    notification_type: NotificationType,
    context: dict[str, t.Any],
    expected_title: str,
    expected_subject: str,
) -> None:
    """Follow templates render actor- and target-aware headers."""
    notification = _make_notification(regular_user, notification_type, context)
    template = get_template(notification_type)

    assert template.get_in_app_title(notification) == expected_title
    assert template.get_email_subject(notification) == expected_subject


@pytest.mark.parametrize(
    ("notification_type", "expected_title", "expected_subject"),
    [
        (
            NotificationType.ORGANIZATION_FOLLOWED,
            "Someone started following your organization",
            "New follower - your organization",
        ),
        (
            NotificationType.EVENT_SERIES_FOLLOWED,
            "Someone started following your series",
            "New follower - your series",
        ),
        (
            NotificationType.NEW_EVENT_FROM_FOLLOWED_ORG,
            "An organization you follow created a new event",
            "New event from An organization you follow",
        ),
        (
            NotificationType.NEW_EVENT_FROM_FOLLOWED_SERIES,
            "New event in A series you follow: a new event",
            "New event in A series you follow",
        ),
    ],
)
def test_follow_templates_fall_back_on_empty_context(
    regular_user: RevelUser,
    notification_type: NotificationType,
    expected_title: str,
    expected_subject: str,
) -> None:
    """Missing context keys fall back to the templates' generic wording."""
    # A non-blank context that carries none of the keys the follow templates read.
    notification = _make_notification(regular_user, notification_type, {"frontend_url": "https://example.com"})
    template = get_template(notification_type)

    assert template.get_in_app_title(notification) == expected_title
    assert template.get_email_subject(notification) == expected_subject


# --- Whitelist templates ---


@pytest.mark.parametrize(
    ("notification_type", "expected_title", "expected_subject"),
    [
        (
            NotificationType.WHITELIST_REQUEST_CREATED,
            "Ada requested verification for Revel",
            "New verification request: Revel",
        ),
        (
            NotificationType.WHITELIST_REQUEST_APPROVED,
            "Verification Approved: Revel",
            "Verification Approved - Revel",
        ),
        (
            NotificationType.WHITELIST_REQUEST_REJECTED,
            "Verification Declined: Revel",
            "Verification Update: Revel",
        ),
    ],
)
def test_whitelist_templates(
    regular_user: RevelUser,
    notification_type: NotificationType,
    expected_title: str,
    expected_subject: str,
) -> None:
    """Whitelist (verification) templates render org-scoped headers."""
    notification = _make_notification(
        regular_user,
        notification_type,
        {"requester_name": "Ada", "organization_name": "Revel"},
    )
    template = get_template(notification_type)

    assert template.get_in_app_title(notification) == expected_title
    assert template.get_email_subject(notification) == expected_subject


# --- Invitation templates ---


@pytest.mark.parametrize(
    ("notification_type", "expected_title", "expected_subject"),
    [
        (
            NotificationType.INVITATION_RECEIVED,
            "You're invited to Launch Party",
            "You're invited: Launch Party",
        ),
        (
            NotificationType.INVITATION_REQUEST_CREATED,
            "ada@example.com requested invitation to Launch Party",
            "New invitation request: Launch Party",
        ),
    ],
)
def test_invitation_templates(
    regular_user: RevelUser,
    notification_type: NotificationType,
    expected_title: str,
    expected_subject: str,
) -> None:
    """Invitation templates render event-scoped headers."""
    notification = _make_notification(
        regular_user,
        notification_type,
        {"event_name": "Launch Party", "requester_email": "ada@example.com"},
    )
    template = get_template(notification_type)

    assert template.get_in_app_title(notification) == expected_title
    assert template.get_email_subject(notification) == expected_subject

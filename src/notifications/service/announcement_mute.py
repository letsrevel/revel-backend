"""Per-organization announcement mute (#1031).

``NotificationPreference.muted_organizations`` is the single source of truth: it governs
``ORG_ANNOUNCEMENT`` on every channel and is enforced where announcements are delivered
(``events.service.announcement_service._deliver_to_recipients``).
"""

from accounts.models import RevelUser
from events.models import Organization
from notifications.models import NotificationPreference


def _preferences(user: RevelUser) -> NotificationPreference:
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)
    return prefs


def mute_organization(user: RevelUser, organization: Organization) -> NotificationPreference:
    """Stop delivering the organization's announcements to the user (idempotent).

    Args:
        user: The user muting.
        organization: The organization to mute.

    Returns:
        The user's notification preferences.
    """
    prefs = _preferences(user)
    prefs.muted_organizations.add(organization)
    return prefs


def unmute_organization(user: RevelUser, organization: Organization) -> NotificationPreference:
    """Resume delivering the organization's announcements to the user (idempotent).

    Args:
        user: The user unmuting.
        organization: The organization to unmute.

    Returns:
        The user's notification preferences.
    """
    prefs = _preferences(user)
    prefs.muted_organizations.remove(organization)
    return prefs


def set_organization_muted(user: RevelUser, organization: Organization, *, muted: bool) -> NotificationPreference:
    """Mute or unmute the organization's announcements for the user.

    Args:
        user: The user.
        organization: The organization.
        muted: Whether announcements should be muted.

    Returns:
        The user's notification preferences.
    """
    if muted:
        return mute_organization(user, organization)
    return unmute_organization(user, organization)

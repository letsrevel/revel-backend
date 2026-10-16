"""Sender identity and List-Unsubscribe headers for organization-pushed email (#1029)."""

from email.utils import formataddr

from django.conf import settings

from common.models import SiteSettings
from common.utils import is_reserved_mailbox, org_email_domain
from events.models import Organization
from notifications.models import Notification

_ORG_FIELDS = ("id", "name", "slug", "contact_email", "contact_email_verified")


def resolve_sender_org(notification: Notification) -> Organization | None:
    """Find the organization an email is sent on behalf of.

    Uses ``context["organization_id"]``, else the organization of ``context["event_id"]``.

    Args:
        notification: The notification being emailed.

    Returns:
        The organization, or None if the context names none or it no longer exists.
    """
    context = notification.context or {}
    orgs = Organization.objects.only(*_ORG_FIELDS)
    if org_id := context.get("organization_id"):
        return orgs.filter(pk=org_id).first()
    if event_id := context.get("event_id"):
        return orgs.filter(events__id=event_id).first()
    return None


def org_from_address(org: Organization) -> str:
    """Build the ``"<Org> via Revel" <slug@ORG_EMAIL_DOMAIN>`` From address.

    Falls back to ``DEFAULT_FROM_EMAIL`` when the slug is a role/RFC 2142 mailbox name.

    Args:
        org: The sending organization.

    Returns:
        A formatted From header value.
    """
    if is_reserved_mailbox(org.slug):
        return str(settings.DEFAULT_FROM_EMAIL)
    name = org.name.replace("\r", " ").replace("\n", " ")
    return formataddr((f"{name} via Revel", f"{org.slug}@{org_email_domain()}"))


def org_reply_to(org: Organization) -> list[str]:
    """Reply-To for org mail: the org's contact address, only once verified.

    Args:
        org: The sending organization.

    Returns:
        ``[contact_email]`` if set and verified, else ``[]``.
    """
    if org.contact_email and org.contact_email_verified:
        return [org.contact_email]
    return []


def build_list_unsubscribe_headers(token: str) -> dict[str, str]:
    """RFC 2369 / RFC 8058 one-click unsubscribe headers (HTTPS only, no mailto).

    Args:
        token: Unsubscribe or opt-out token the one-click endpoint dispatches on.

    Returns:
        ``List-Unsubscribe`` and ``List-Unsubscribe-Post`` headers.
    """
    url = f"{settings.BASE_URL.rstrip('/')}/api/notification-preferences/one-click?token={token}"
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def build_preferences_list_unsubscribe_header(token: str) -> dict[str, str]:
    """``List-Unsubscribe`` header for mail the recipient cannot opt out of.

    Brevo injects its own one-click header into any email that lacks one, and a client
    "Unsubscribe" click would then globally block even mandatory mail. This header points
    at the frontend preferences page instead. There is deliberately no
    ``List-Unsubscribe-Post``: without it clients open the URL in a browser rather than
    POSTing silently, so nothing is switched off by the click.

    Args:
        token: Typed unsubscribe token the frontend ``/unsubscribe`` page consumes.

    Returns:
        A ``List-Unsubscribe`` header only.
    """
    url = f"{SiteSettings.get_solo().frontend_base_url}/unsubscribe?token={token}"
    return {"List-Unsubscribe": f"<{url}>"}

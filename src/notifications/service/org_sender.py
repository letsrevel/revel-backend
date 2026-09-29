"""Sender identity and List-Unsubscribe headers for organization-pushed email (#1029)."""

from email.utils import formataddr

from django.conf import settings

from common.utils import org_email_domain
from events.models import Organization
from notifications.models import Notification

# RFC 2142 / role mailboxes an org slug must never impersonate on our domain.
_RESERVED_LOCAL_PARTS = frozenset(
    {
        "abuse",
        "postmaster",
        "hostmaster",
        "webmaster",
        "security",
        "noreply",
        "no-reply",
        "support",
        "admin",
        "root",
        "mailer-daemon",
        "info",
        "billing",
        "revel",
    }
)

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
    if org.slug.lower() in _RESERVED_LOCAL_PARTS:
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

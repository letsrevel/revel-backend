"""Emails to the organization owner when a custom domain goes live or fails (white-label, #1148).

Called by ``custom_domain_service._save_status``, the single writer of a domain's status, so the
scheduled sync, **Check now** and the Django admin actions all notify through one place, once per
transition. Only two transitions matter to the owner:

- PENDING -> ACTIVE: the domain is live (``ORG_CUSTOM_DOMAIN_ACTIVE``).
- PENDING or ACTIVE -> FAILED: it does not work (``ORG_CUSTOM_DOMAIN_FAILED``).

REMOVED is never notified (the owner asked for it), nor is a re-activation of a FAILED domain (only
staff can do that, from the admin) or any no-op. An admin action that causes a transition does notify:
without Cloudflare configured that action *is* the activation, and the owner is otherwise left
polling the settings page.

Recipient: the organization owner only. The custom-domain endpoints are owner-only, so staff have
nothing to act on; platform staff have their own alert (``notify_custom_domain_problem``). The mail is
opt-out-able like any other owner notification, not mandatory: it carries no money, security or legal
consequence for the recipient, and the settings page always shows the same state.
"""

import typing as t

from django.db import transaction

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Organization, OrganizationDomain
from events.utils.frontend_urls import frontend_base_url_for_domain
from notifications.enums import NotificationType

Status = OrganizationDomain.Status


def admin_settings_url(organization: Organization) -> str:
    """Link to the organization's custom-domain settings page, always on the platform origin."""
    base = SiteSettings.get_solo().frontend_base_url.rstrip("/")
    return f"{base}/org/{organization.slug}/admin/custom-domain"


def _notification_for(previous: str, new: str) -> NotificationType | None:
    """The owner notification a ``previous`` -> ``new`` status change warrants, if any."""
    if new == Status.ACTIVE and previous == Status.PENDING:
        return NotificationType.ORG_CUSTOM_DOMAIN_ACTIVE
    if new == Status.FAILED and previous in (Status.PENDING, Status.ACTIVE):
        return NotificationType.ORG_CUSTOM_DOMAIN_FAILED
    return None


def notify_owner_of_transition(
    domain: OrganizationDomain,
    organization: Organization,
    previous: str,
    new: str,
) -> None:
    """Email the organization owner about a domain status change, once it has been committed.

    The notification is requested from an ``on_commit`` hook, so nothing is sent for a rolled-back
    transition. Call it after the new status was written.

    Args:
        domain: The domain, already carrying its new status and ``last_errors``.
        organization: The domain's organization.
        previous: The status before the change.
        new: The status after the change.
    """
    notification_type = _notification_for(previous, new)
    if notification_type is None:
        return
    context: dict[str, t.Any] = {
        "organization_id": str(organization.pk),
        "organization_name": organization.name,
        "organization_slug": organization.slug,
        "hostname": domain.hostname,
        "action_url": admin_settings_url(organization),
    }
    if notification_type == NotificationType.ORG_CUSTOM_DOMAIN_ACTIVE:
        context["domain_url"] = frontend_base_url_for_domain(domain.hostname, True)
        context["frontend_url"] = context["domain_url"]
    else:
        context["was_live"] = previous == Status.ACTIVE
        context["errors"] = [str(message) for message in domain.last_errors]
        context["frontend_url"] = context["action_url"]
    owner_id = organization.owner_id

    def send() -> None:
        from notifications.service.notification_helpers import notify_org_staff

        owner = RevelUser.objects.select_related("notification_preferences").get(pk=owner_id)
        notify_org_staff(
            organization_id=organization.pk,
            notification_type=notification_type,
            context=context,
            sender=OrganizationDomain,
            recipients=[owner],
        )

    transaction.on_commit(send)

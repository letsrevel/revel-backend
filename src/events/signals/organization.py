"""Receivers for Organization, reserved slug tokens and tenant CORS."""

import typing as t

import structlog
from corsheaders.signals import check_request_enabled
from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver
from django.http import HttpRequest

from common.models import SiteSettings
from events.models import (
    Organization,
    ReservedSlugToken,
)
from events.models.membership import MembershipTier
from events.service import (
    turnstile_hostname_service,
)
from events.service.tenant_service import invalidate_tenant_cache, tenant_host_from_origin
from events.tasks import (
    delete_organization_cloudflare_hostnames,
    notify_admin_new_organization_discord,
    notify_admin_new_organization_pushover,
)
from events.utils.reserved_slug_tokens import invalidate_reserved_tokens_cache

# The logger name of the module before the split, so the log names stay the same.
logger = structlog.get_logger("events.signals")


@receiver(post_save, sender=Organization)
def handle_organization_creation(
    sender: type[Organization], instance: Organization, created: bool, **kwargs: t.Any
) -> None:
    """Create default 'General membership' tier and notify admin when an organization is created."""
    if not created:
        return

    MembershipTier.objects.create(organization=instance, name="General membership")
    logger.info(
        "default_membership_tier_created",
        organization_id=str(instance.id),
        organization_name=instance.name,
    )

    if not SiteSettings.get_solo().notify_organization_created:
        return

    organization_id = str(instance.id)

    def _dispatch_admin_notifications() -> None:
        notify_admin_new_organization_pushover.delay(organization_id=organization_id)
        notify_admin_new_organization_discord.delay(organization_id=organization_id)

    transaction.on_commit(_dispatch_admin_notifications)


@receiver(post_save, sender=ReservedSlugToken)
def _invalidate_reserved_slug_token_cache_on_save(
    sender: type[ReservedSlugToken],
    instance: ReservedSlugToken,
    **kwargs: t.Any,
) -> None:
    invalidate_reserved_tokens_cache()


@receiver(post_delete, sender=ReservedSlugToken)
def _invalidate_reserved_slug_token_cache_on_delete(
    sender: type[ReservedSlugToken],
    instance: ReservedSlugToken,
    **kwargs: t.Any,
) -> None:
    invalidate_reserved_tokens_cache()


@receiver(check_request_enabled)
def allow_cors_for_tenant_origin(sender: None, request: HttpRequest, **kwargs: t.Any) -> bool:
    """Allow cross-origin requests from an organization's active custom domain (white-label, #1152).

    django-cors-headers calls this when the static ``CORS_ALLOWED_ORIGINS`` do not match; a truthy
    result enables CORS for the request, with the usual credentials and allowed-header settings.
    The Origin host (port ignored) must be an active tenant domain, over ``https`` (``http`` only
    when ``DEBUG``). No CORS when the organization's ``custom_domain_enabled`` kill switch is off.
    """
    return tenant_host_from_origin(request.headers.get("origin")) is not None


@receiver(pre_delete, sender=Organization)
def capture_cloudflare_hostnames_before_organization_delete(
    sender: type[Organization], instance: Organization, **kwargs: t.Any
) -> None:
    """Remember the organization's Cloudflare hostname ids: the cascade removes the domain rows after this."""
    instance._cloudflare_hostname_ids = list(  # type: ignore[attr-defined]
        instance.domains.filter(cloudflare_hostname_id__isnull=False).values_list("cloudflare_hostname_id", flat=True)
    )


@receiver(post_delete, sender=Organization)
def invalidate_tenant_cache_on_organization_delete(
    sender: type[Organization], instance: Organization, **kwargs: t.Any
) -> None:
    """Drop the tenant-resolution entry of a deleted organization's custom domain (white-label, #1152).

    Deleting the organization cascades its domains without going through the domain service, so the
    cached hostname would otherwise keep resolving to a vanished organization until its TTL ran out.
    """
    if hostname := instance.custom_domain:
        transaction.on_commit(lambda: invalidate_tenant_cache(hostname))
        turnstile_hostname_service.schedule_reconcile()  # its ACTIVE hostname leaves the Turnstile widget
    # The cascade also orphans the Cloudflare hostnames (certificates, claimed hostname): remove them
    # once the deletion commits. A task, so the request never waits on (or fails because of) Cloudflare.
    if hostname_ids := getattr(instance, "_cloudflare_hostname_ids", None):
        transaction.on_commit(lambda: delete_organization_cloudflare_hostnames.delay(hostname_ids))

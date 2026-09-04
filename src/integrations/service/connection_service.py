"""Org-level platform connection lifecycle (spec §6). Function-based: stateless, request-scoped args."""

import typing as t
from dataclasses import dataclass

import structlog
from django.conf import settings
from django.utils.translation import gettext_lazy as _

from accounts.models import RevelUser
from events.models import Organization
from integrations import registry
from integrations.exceptions import IntegrationError, ProviderError
from integrations.models import PlatformConnection
from integrations.providers.base import ListingProvider, RemoteAccount
from integrations.schema import ConnectionSchema, IntegrationErrorCode, IntegrationErrorSchema
from integrations.service import state as state_service

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ConnectStart:
    authorize_url: str
    state: str


def redirect_uri(provider_key: str) -> str:
    """The callback URL registered on the provider app (must match its hostname)."""
    return f"{settings.BASE_URL}/api/integrations/{provider_key}/callback"


def webhook_url(provider_key: str, secret: str) -> str:
    """The per-connection webhook endpoint; the secret path segment hides it from strangers."""
    return f"{settings.BASE_URL}/api/integrations/{provider_key}/webhook/{secret}"


def _provider_error(e: ProviderError, message: str) -> IntegrationError:
    status = (
        502 if e.code in (IntegrationErrorCode.PROVIDER_REJECTED, IntegrationErrorCode.PROVIDER_RATE_LIMITED) else 400
    )
    return IntegrationError(e.code, message, e.provider_message, status=status)


def get_connection(organization: Organization, provider_key: str) -> PlatformConnection:
    """The org's connection for ``provider_key`` or a 404-mapped error."""
    registry.get_provider(provider_key)  # 404 for unknown/disabled providers
    conn = PlatformConnection.objects.filter(organization=organization, provider=provider_key).first()
    if conn is None:
        raise IntegrationError(
            IntegrationErrorCode.PROVIDER_NOT_CONNECTED, str(_("This platform is not connected.")), status=404
        )
    return conn


def list_connections(organization: Organization) -> list[ConnectionSchema]:
    """One row per enabled provider, connected or not."""
    existing = {c.provider: c for c in PlatformConnection.objects.filter(organization=organization)}
    rows: list[ConnectionSchema] = []
    for provider in registry.enabled_providers():
        conn = existing.get(provider.key)
        rows.append(
            ConnectionSchema(
                provider=provider.key,
                display_name=provider.display_name,
                status=t.cast(t.Any, conn.status) if conn else None,
                remote_account_name=conn.remote_account_name if conn else "",
                auto_sync=conn.auto_sync if conn else False,
                last_error=IntegrationErrorSchema(**conn.last_error) if conn and conn.last_error else None,
                connected_at=conn.created_at if conn else None,
            )
        )
    return rows


def begin_connect(organization: Organization, user: RevelUser, provider_key: str) -> ConnectStart:
    """Mint the state and build the provider's authorize URL. Refuses if already active."""
    provider = registry.get_provider(provider_key)
    conn = PlatformConnection.objects.filter(organization=organization, provider=provider_key).first()
    if conn is not None and conn.status == PlatformConnection.Status.ACTIVE:
        raise IntegrationError(
            IntegrationErrorCode.ALREADY_CONNECTED, str(_("This platform is already connected.")), status=409
        )
    state = state_service.mint_state(organization_id=organization.id, user_id=user.id, provider=provider_key)
    logger.info("integration_connect_started", organization_id=str(organization.id), provider=provider_key)
    return ConnectStart(authorize_url=provider.authorize_url(state, redirect_uri(provider_key)), state=state)


def _register_webhook(conn: PlatformConnection, provider: ListingProvider) -> None:
    """Best effort: a failed registration is recorded, never fatal (counts fall back to reconcile)."""
    try:
        conn.webhook_remote_id = provider.register_webhook(
            conn.token(), conn.remote_account_id, webhook_url(provider.key, conn.webhook_secret)
        )
        conn.save(update_fields=["webhook_remote_id", "updated_at"])
    except ProviderError as e:
        logger.warning("integration_webhook_registration_failed", provider=provider.key, error=e.provider_message)
        conn.record_error(
            IntegrationErrorCode.WEBHOOK_REGISTRATION_FAILED,
            str(_("Live updates could not be enabled; counts will refresh periodically.")),
            e.provider_message,
        )


def _bind_account(conn: PlatformConnection, provider: ListingProvider, account: RemoteAccount) -> PlatformConnection:
    conn.remote_account_id = account.remote_id
    conn.remote_account_name = account.name
    conn.status = PlatformConnection.Status.ACTIVE
    conn.last_error = None
    conn.save(update_fields=["remote_account_id", "remote_account_name", "status", "last_error", "updated_at"])
    _register_webhook(conn, provider)
    logger.info("integration_connected", provider=provider.key, organization_id=str(conn.organization_id))
    return conn


def complete_connect(state: str, code: str) -> PlatformConnection:
    """Callback half: verify state + ownership, exchange the code, bind the account (or leave pending)."""
    payload = state_service.validate_state(state)
    provider = registry.get_provider(payload.provider)
    organization = Organization.objects.filter(id=payload.organization_id, owner_id=payload.user_id).first()
    if organization is None:
        raise IntegrationError(
            IntegrationErrorCode.STATE_INVALID,
            str(_("The connection request is invalid or has expired. Please try again.")),
        )
    try:
        token = provider.exchange_code(code, redirect_uri(provider.key))
        accounts = provider.list_accounts(token)
    except ProviderError as e:
        raise _provider_error(e, str(_("The platform rejected the connection."))) from e
    conn, _created = PlatformConnection.objects.update_or_create(
        organization=organization,
        provider=provider.key,
        defaults={
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
            "token_expires_at": token.expires_at,
            "status": PlatformConnection.Status.PENDING,
            "last_error": None,
            "remote_account_id": "",
            "remote_account_name": "",
            "webhook_remote_id": "",
        },
    )
    # Runs inside the request transaction under ATOMIC_REQUESTS by accepted trade-off (see the
    # callback docstring in controllers/public.py); a failure rolls the connection row back.
    if len(accounts) == 1:
        return _bind_account(conn, provider, accounts[0])
    return conn


def list_pending_accounts(organization: Organization, provider_key: str) -> list[RemoteAccount]:
    """Accounts the pending token can see, for the picker."""
    conn = get_connection(organization, provider_key)
    if conn.status != PlatformConnection.Status.PENDING:
        raise IntegrationError(
            IntegrationErrorCode.CONNECTION_PENDING, str(_("No account selection is pending.")), status=409
        )
    try:
        return registry.get_provider(provider_key).list_accounts(conn.token())
    except ProviderError as e:
        raise _provider_error(e, str(_("The platform rejected the request."))) from e


def select_account(organization: Organization, provider_key: str, remote_id: str) -> PlatformConnection:
    """Bind one of the pending accounts and activate."""
    conn = get_connection(organization, provider_key)
    accounts = list_pending_accounts(organization, provider_key)
    match = next((a for a in accounts if a.remote_id == remote_id), None)
    if match is None:
        raise IntegrationError(
            IntegrationErrorCode.ACCOUNT_UNKNOWN, str(_("That account is not available to this connection."))
        )
    return _bind_account(conn, registry.get_provider(provider_key), match)


def set_auto_sync(organization: Organization, provider_key: str, auto_sync: bool) -> PlatformConnection:
    """Flip the org-wide auto-sync default."""
    conn = get_connection(organization, provider_key)
    conn.auto_sync = auto_sync
    conn.save(update_fields=["auto_sync", "updated_at"])
    return conn


def disconnect(organization: Organization, provider_key: str) -> None:
    """Unregister the webhook, revoke (provider permitting), delete the connection. Links cascade."""
    conn = get_connection(organization, provider_key)
    provider = registry.get_provider(provider_key)
    try:
        if conn.webhook_remote_id:
            provider.unregister_webhook(conn.token(), conn.webhook_remote_id)
        provider.revoke(conn.token())
    except ProviderError as e:  # a dead token must not block cleanup
        logger.warning("integration_disconnect_provider_error", provider=provider_key, error=e.provider_message)
    conn.delete()
    logger.info("integration_disconnected", provider=provider_key, organization_id=str(organization.id))


def mark_revoked(connection: PlatformConnection) -> None:
    """Provider answered 401: flag once, stop auto sync until the owner reconnects (spec §6.5)."""
    connection.record_error(
        IntegrationErrorCode.CONNECTION_REVOKED,
        str(_("The platform connection is no longer valid. Please reconnect.")),
        status=PlatformConnection.Status.ERROR,
    )

# src/telegram/middleware.py

import dataclasses
import typing as t
import uuid

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as AiogramUser
from django.db import models as django_models

from accounts.models import RevelUser
from events.models import EventInvitationRequest, Organization, OrganizationStaff, WhitelistRequest
from telegram.models import TelegramUser
from telegram.utils import get_or_create_tg_user

logger = structlog.get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class _StaffPermissionConfig:
    resolve: t.Callable[[uuid.UUID], t.Awaitable[django_models.Model | None]]
    get_organization: t.Callable[[t.Any], Organization]
    deny_message: str


async def _resolve_invitation_request(pk: uuid.UUID) -> django_models.Model | None:
    return await EventInvitationRequest.objects.select_related("event__organization", "user").filter(pk=pk).afirst()


async def _resolve_whitelist_request(pk: uuid.UUID) -> django_models.Model | None:
    return await WhitelistRequest.objects.select_related("organization", "user").filter(pk=pk).afirst()


_STAFF_PERMISSION_CONFIGS: dict[str, _StaffPermissionConfig] = {
    "invitation_request": _StaffPermissionConfig(
        resolve=_resolve_invitation_request,
        get_organization=lambda r: r.event.organization,
        deny_message="You no longer have permission to manage invitations.",
    ),
    "whitelist_request": _StaffPermissionConfig(
        resolve=_resolve_whitelist_request,
        get_organization=lambda r: r.organization,
        deny_message="You no longer have permission to manage members.",
    ),
}


async def _check_staff_permission(user: RevelUser, organization: Organization, action: str) -> bool:
    """Check if a user has a specific staff permission on an organization."""
    if organization.owner_id == user.id:
        return True
    staff = await OrganizationStaff.objects.filter(organization=organization, user=user).afirst()
    if staff is None:
        return False
    return staff.has_permission(action)


class TelegramUserMiddleware(BaseMiddleware):
    """Outer middleware: Creates/fetches TelegramUser for every update.

    Injects 'tg_user' into handler data with prefetched user relationship.
    This middleware runs on every incoming update before filters are evaluated.
    """

    async def __call__(
        self,
        handler: t.Callable[[TelegramObject, t.Dict[str, t.Any]], t.Awaitable[t.Any]],
        event: TelegramObject,
        data: t.Dict[str, t.Any],
    ) -> t.Any:
        """Inject TelegramUser into context."""
        aiogram_user: AiogramUser | None = data.get("event_from_user")

        if not aiogram_user:
            logger.error("telegram_user_not_in_event_data")
            return None  # Stop processing

        tg_user: TelegramUser = await get_or_create_tg_user(aiogram_user)

        # Mark if bot was unblocked or user was reactivated
        if tg_user.blocked_by_user or tg_user.user_is_deactivated:
            tg_user.blocked_by_user = False
            tg_user.user_is_deactivated = False
            await tg_user.asave(update_fields=["blocked_by_user", "user_is_deactivated", "updated_at"])

        data["tg_user"] = tg_user
        return await handler(event, data)


class AuthorizationMiddleware(BaseMiddleware):
    """Inner middleware: Enforces authorization flags.

    Checks handler flags for:
    - requires_linked_user: Injects 'user' if linked, sends error if not
    - requires_superuser: Ensures user is superuser
    - staff_permission: Permission action to check (e.g. "invite_to_event").
      Requires ``permission_entity`` flag to resolve the target organization.
      Implies ``requires_linked_user``. Injects 'checked_entity' into handler data.

    This middleware runs after filters pass, before the handler is invoked.
    """

    @staticmethod
    async def _send_error(event: TelegramObject, message: str, alert: str) -> None:
        """Send an authorization error to the user via message or callback alert."""
        if isinstance(event, Message):
            await event.answer(message)
        elif isinstance(event, CallbackQuery):
            await event.answer(alert, show_alert=True)

    async def __call__(
        self,
        handler: t.Callable[[TelegramObject, t.Dict[str, t.Any]], t.Awaitable[t.Any]],
        event: TelegramObject,
        data: t.Dict[str, t.Any],
    ) -> t.Any:
        """Enforce authorization based on handler flags."""
        from aiogram.dispatcher.flags import get_flag

        tg_user: TelegramUser | None = data.get("tg_user")

        if not tg_user:
            logger.error("telegram_user_not_in_context")
            return None

        requires_linked = get_flag(data, "requires_linked_user", default=False)
        requires_superuser = get_flag(data, "requires_superuser", default=False)
        staff_permission: str | None = get_flag(data, "staff_permission")

        # staff_permission implies requires_linked_user
        if not (requires_linked or requires_superuser or staff_permission):
            return await handler(event, data)

        if not tg_user.user_id:
            await self._send_error(
                event,
                "⚠️ Please link your Revel account first using /connect command.",
                "Please link your account first using /connect",
            )
            return None

        user: RevelUser = t.cast(RevelUser, tg_user.user)

        if requires_superuser and not user.is_superuser:
            await self._send_error(event, "⚠️ This command is for administrators only.", "Administrators only")
            return None

        data["user"] = user

        if staff_permission and isinstance(event, CallbackQuery) and event.data:
            if not await self._enforce_staff_permission(event, data, staff_permission):
                return None

        return await handler(event, data)

    @staticmethod
    async def _enforce_staff_permission(event: CallbackQuery, data: t.Dict[str, t.Any], staff_permission: str) -> bool:
        """Check staff permission for organizer callback actions.

        Returns True if the user has the required permission, False otherwise.
        On success, injects 'checked_entity' into the handler data dict.
        """
        from aiogram.dispatcher.flags import get_flag

        user: RevelUser = data["user"]
        permission_entity: str | None = get_flag(data, "permission_entity")
        if not permission_entity or permission_entity not in _STAFF_PERMISSION_CONFIGS:
            logger.error(
                "invalid_permission_entity_flag",
                permission_entity=permission_entity,
                staff_permission=staff_permission,
            )
            return False

        config = _STAFF_PERMISSION_CONFIGS[permission_entity]
        assert event.data is not None
        try:
            entity_id = uuid.UUID(event.data.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            logger.warning("malformed_callback_data", callback_data=event.data)
            return False

        entity = await config.resolve(entity_id)
        if entity is None:
            await event.answer("❌ Request not found.", show_alert=True)
            return False

        organization = config.get_organization(entity)
        if not await _check_staff_permission(user, organization, staff_permission):
            await event.answer(f"❌ {config.deny_message}", show_alert=True)
            return False

        data["checked_entity"] = entity
        return True

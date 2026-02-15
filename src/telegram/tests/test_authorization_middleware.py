# src/telegram/tests/test_authorization_middleware.py
import typing as t
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as AiogramUser

from accounts.models import RevelUser
from events.models import Event, EventInvitationRequest, Organization, OrganizationStaff, WhitelistRequest
from telegram.middleware import AuthorizationMiddleware
from telegram.models import TelegramUser

pytestmark = pytest.mark.django_db


# ── helpers ──────────────────────────────────────────────────────────


async def _get_tg_user(user: RevelUser) -> TelegramUser:
    return await TelegramUser.objects.select_related("user").aget(user=user)


async def _make_unlinked_tg_user(telegram_id: int) -> TelegramUser:
    tg_user, _ = await TelegramUser.objects.aget_or_create(
        telegram_id=telegram_id,
        defaults={"telegram_username": "unlinked"},
    )
    return tg_user


def _make_flags_getter(flags: dict[str, t.Any]) -> t.Callable[..., t.Any]:
    """Build a side_effect for ``get_flag`` that reads from the given dict."""

    def _get_flag(data: t.Any, flag: str, default: t.Any = None) -> t.Any:
        return flags.get(flag, default)

    return _get_flag


def _make_msg_event() -> AsyncMock:
    """Create a mock Message that passes isinstance checks."""
    event = AsyncMock(spec=Message)
    event.answer = AsyncMock(name="Message.answer")
    return event


def _make_cb_event(data: str = "") -> AsyncMock:
    """Create a mock CallbackQuery that passes isinstance checks."""
    event = AsyncMock(spec=CallbackQuery)
    event.answer = AsyncMock(name="CallbackQuery.answer")
    event.data = data
    return event


@pytest.fixture
def middleware() -> AuthorizationMiddleware:
    return AuthorizationMiddleware()


# ── no flags (pass-through) ─────────────────────────────────────────


class TestNoFlags:
    @pytest.mark.asyncio
    async def test_no_flags_passes_through(
        self,
        middleware: AuthorizationMiddleware,
        django_user: RevelUser,
    ) -> None:
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_user)
        event = _make_msg_event()
        data: dict[str, t.Any] = {"tg_user": tg_user}

        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter({})):
            await middleware(handler, event, data)

        handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_tg_user_stops(
        self,
        middleware: AuthorizationMiddleware,
    ) -> None:
        handler = AsyncMock()
        event = _make_msg_event()
        data: dict[str, t.Any] = {}

        result = await middleware(handler, event, data)

        assert result is None
        handler.assert_not_awaited()


# ── requires_linked_user ─────────────────────────────────────────────


class TestRequiresLinkedUser:
    @pytest.mark.asyncio
    async def test_linked_user_passes(
        self,
        middleware: AuthorizationMiddleware,
        django_user: RevelUser,
    ) -> None:
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_user)
        event = _make_msg_event()
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {"requires_linked_user": True}
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_awaited_once()
        assert data["user"] == django_user

    @pytest.mark.asyncio
    async def test_unlinked_user_blocked_message(
        self,
        middleware: AuthorizationMiddleware,
        aiogram_user: AiogramUser,
    ) -> None:
        handler = AsyncMock()
        tg_user = await _make_unlinked_tg_user(aiogram_user.id)
        event = _make_msg_event()
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {"requires_linked_user": True}
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            result = await middleware(handler, event, data)

        assert result is None
        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        assert "/connect" in event.answer.call_args.args[0]

    @pytest.mark.asyncio
    async def test_unlinked_user_blocked_callback(
        self,
        middleware: AuthorizationMiddleware,
        aiogram_user: AiogramUser,
    ) -> None:
        """Callback queries show alert instead of text message."""
        handler = AsyncMock()
        tg_user = await _make_unlinked_tg_user(aiogram_user.id)
        event = _make_cb_event(data="some:action")
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {"requires_linked_user": True}
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            result = await middleware(handler, event, data)

        assert result is None
        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        assert event.answer.call_args.kwargs["show_alert"] is True


# ── requires_superuser ───────────────────────────────────────────────


class TestRequiresSuperuser:
    @pytest.mark.asyncio
    async def test_superuser_passes(
        self,
        middleware: AuthorizationMiddleware,
        django_superuser: RevelUser,
    ) -> None:
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_superuser)
        event = _make_msg_event()
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {"requires_superuser": True}
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_awaited_once()
        assert data["user"] == django_superuser

    @pytest.mark.asyncio
    async def test_non_superuser_blocked(
        self,
        middleware: AuthorizationMiddleware,
        django_user: RevelUser,
    ) -> None:
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_user)
        event = _make_msg_event()
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {"requires_superuser": True}
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            result = await middleware(handler, event, data)

        assert result is None
        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        assert "administrators" in event.answer.call_args.args[0].lower()


# ── staff_permission ─────────────────────────────────────────────────


class TestStaffPermission:
    @pytest.mark.asyncio
    async def test_owner_has_permission(
        self,
        middleware: AuthorizationMiddleware,
        django_superuser: RevelUser,
        django_user: RevelUser,
        organization: Organization,
        private_event: Event,
    ) -> None:
        """Organization owner should pass staff permission checks."""
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_superuser)  # django_superuser owns the org

        request = await EventInvitationRequest.objects.acreate(event=private_event, user=django_user)
        event = _make_cb_event(data=f"invitation_request_accept:{request.pk}")
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {
            "requires_linked_user": True,
            "staff_permission": "invite_to_event",
            "permission_entity": "invitation_request",
        }
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_awaited_once()
        assert "checked_entity" in data
        assert data["checked_entity"].pk == request.pk

    @pytest.mark.asyncio
    async def test_staff_with_permission_passes(
        self,
        middleware: AuthorizationMiddleware,
        django_user: RevelUser,
        django_superuser: RevelUser,
        organization: Organization,
        private_event: Event,
    ) -> None:
        """Staff member with the required permission should pass."""
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_user)

        # Make django_user a staff member (default perms include invite_to_event=True)
        await OrganizationStaff.objects.acreate(
            organization=organization,
            user=django_user,
        )

        request = await EventInvitationRequest.objects.acreate(event=private_event, user=django_superuser)
        event = _make_cb_event(data=f"invitation_request_accept:{request.pk}")
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {
            "requires_linked_user": True,
            "staff_permission": "invite_to_event",
            "permission_entity": "invitation_request",
        }
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_awaited_once()
        assert "checked_entity" in data

    @pytest.mark.asyncio
    async def test_non_staff_blocked(
        self,
        middleware: AuthorizationMiddleware,
        django_user: RevelUser,
        django_superuser: RevelUser,
        organization: Organization,
        private_event: Event,
    ) -> None:
        """Non-staff user should be blocked."""
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_user)

        request = await EventInvitationRequest.objects.acreate(event=private_event, user=django_superuser)
        event = _make_cb_event(data=f"invitation_request_accept:{request.pk}")
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {
            "requires_linked_user": True,
            "staff_permission": "invite_to_event",
            "permission_entity": "invitation_request",
        }
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        assert "permission" in event.answer.call_args.args[0].lower()
        assert event.answer.call_args.kwargs["show_alert"] is True

    @pytest.mark.asyncio
    async def test_entity_not_found(
        self,
        middleware: AuthorizationMiddleware,
        django_superuser: RevelUser,
        organization: Organization,
    ) -> None:
        """Non-existent entity ID should show 'not found'."""
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_superuser)
        fake_id = uuid.uuid4()
        event = _make_cb_event(data=f"invitation_request_accept:{fake_id}")
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {
            "requires_linked_user": True,
            "staff_permission": "invite_to_event",
            "permission_entity": "invitation_request",
        }
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_not_awaited()
        event.answer.assert_awaited_once()
        assert "not found" in event.answer.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_malformed_callback_data(
        self,
        middleware: AuthorizationMiddleware,
        django_superuser: RevelUser,
    ) -> None:
        """Malformed callback data (no UUID) should silently fail."""
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_superuser)
        event = _make_cb_event(data="invitation_request_accept:not-a-uuid")
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {
            "requires_linked_user": True,
            "staff_permission": "invite_to_event",
            "permission_entity": "invitation_request",
        }
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_whitelist_request_permission(
        self,
        middleware: AuthorizationMiddleware,
        django_user: RevelUser,
        django_superuser: RevelUser,
        organization: Organization,
    ) -> None:
        """Test the whitelist_request permission_entity path."""
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_superuser)  # owner

        request = await WhitelistRequest.objects.acreate(organization=organization, user=django_user)
        event = _make_cb_event(data=f"whitelist_request_approve:{request.pk}")
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {
            "requires_linked_user": True,
            "staff_permission": "manage_members",
            "permission_entity": "whitelist_request",
        }
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_awaited_once()
        assert data["checked_entity"].pk == request.pk

    @pytest.mark.asyncio
    async def test_invalid_permission_entity(
        self,
        middleware: AuthorizationMiddleware,
        django_superuser: RevelUser,
    ) -> None:
        """Invalid permission_entity flag should silently fail."""
        handler = AsyncMock()
        tg_user = await _get_tg_user(django_superuser)
        event = _make_cb_event(data=f"something:{uuid.uuid4()}")
        data: dict[str, t.Any] = {"tg_user": tg_user}

        flags = {
            "requires_linked_user": True,
            "staff_permission": "some_action",
            "permission_entity": "nonexistent_entity",
        }
        with patch("aiogram.dispatcher.flags.get_flag", side_effect=_make_flags_getter(flags)):
            await middleware(handler, event, data)

        handler.assert_not_awaited()

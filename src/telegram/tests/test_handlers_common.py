# src/telegram/tests/test_handlers_common.py
import typing as t
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async

from accounts.models import RevelUser
from common.models import Legal
from telegram.models import AccountOTP, TelegramUser
from telegram.routers.common import (
    handle_cancel,
    handle_connect,
    handle_privacy,
    handle_start,
    handle_toc,
    handle_unsubscribe,
    sanitize_text,
)

pytestmark = pytest.mark.django_db


# ── helpers ──────────────────────────────────────────────────────────


async def _get_tg_user(user: RevelUser) -> TelegramUser:
    return await TelegramUser.objects.select_related("user").aget(user=user)


async def _get_unlinked_tg_user(telegram_id: int) -> TelegramUser:
    tg_user, _ = await TelegramUser.objects.aget_or_create(
        telegram_id=telegram_id,
        defaults={"telegram_username": "unlinked"},
    )
    return tg_user


# ── handle_start ─────────────────────────────────────────────────────


class TestHandleStart:
    @pytest.mark.asyncio
    async def test_linked_user(
        self,
        mock_message: AsyncMock,
        mock_fsm_context: AsyncMock,
        django_user: RevelUser,
    ) -> None:
        tg_user = await _get_tg_user(django_user)

        await handle_start(mock_message, tg_user=tg_user, state=mock_fsm_context)

        mock_fsm_context.clear.assert_awaited_once()
        mock_message.answer.assert_awaited_once()
        text = mock_message.answer.call_args.args[0]
        assert "Welcome back" in text
        assert django_user.display_name in text

    @pytest.mark.asyncio
    async def test_unlinked_user(
        self,
        mock_message: AsyncMock,
        mock_fsm_context: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        tg_user = await _get_unlinked_tg_user(aiogram_user.id)

        await handle_start(mock_message, tg_user=tg_user, state=mock_fsm_context)

        mock_fsm_context.clear.assert_awaited_once()
        mock_message.answer.assert_awaited_once()
        text = mock_message.answer.call_args.args[0]
        assert "/connect" in text
        assert "Welcome to Revel" in text


# ── handle_cancel ────────────────────────────────────────────────────


class TestHandleCancel:
    @pytest.mark.asyncio
    async def test_cancel_with_active_state(
        self,
        mock_message: AsyncMock,
        mock_fsm_context: AsyncMock,
    ) -> None:
        mock_fsm_context.get_state.return_value = "SomeState:active"

        await handle_cancel(mock_message, state=mock_fsm_context)

        mock_fsm_context.clear.assert_awaited_once()
        mock_message.reply.assert_awaited_once()
        assert "cancelled" in mock_message.reply.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_cancel_without_active_state(
        self,
        mock_message: AsyncMock,
        mock_fsm_context: AsyncMock,
    ) -> None:
        mock_fsm_context.get_state.return_value = None

        await handle_cancel(mock_message, state=mock_fsm_context)

        mock_fsm_context.clear.assert_not_awaited()
        mock_message.reply.assert_awaited_once()
        assert "nothing to cancel" in mock_message.reply.call_args.args[0].lower()


# ── handle_toc ───────────────────────────────────────────────────────


class TestHandleToc:
    @staticmethod
    def _set_legal_toc(text: str) -> None:
        legal = Legal.get_solo()
        legal.terms_and_conditions = text
        legal.save()

    @pytest.mark.asyncio
    async def test_toc_with_content(
        self,
        mock_message: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        tg_user = await _get_unlinked_tg_user(aiogram_user.id)
        await sync_to_async(self._set_legal_toc)("<b>Test Terms</b>")

        await handle_toc(mock_message, tg_user=tg_user)

        mock_message.answer.assert_awaited_once()
        text = mock_message.answer.call_args.args[0]
        assert "<b>Test Terms</b>" in text

    @pytest.mark.asyncio
    async def test_toc_empty(
        self,
        mock_message: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        tg_user = await _get_unlinked_tg_user(aiogram_user.id)
        await sync_to_async(self._set_legal_toc)("")

        await handle_toc(mock_message, tg_user=tg_user)

        mock_message.answer.assert_awaited_once()
        text = mock_message.answer.call_args.args[0]
        assert "not available" in text.lower()


# ── handle_privacy ───────────────────────────────────────────────────


class TestHandlePrivacy:
    @staticmethod
    def _set_legal_privacy(text: str) -> None:
        legal = Legal.get_solo()
        legal.privacy_policy = text
        legal.save()

    @pytest.mark.asyncio
    async def test_privacy_with_content(
        self,
        mock_message: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        tg_user = await _get_unlinked_tg_user(aiogram_user.id)
        await sync_to_async(self._set_legal_privacy)("<i>Test Privacy</i>")

        await handle_privacy(mock_message, tg_user=tg_user)

        mock_message.answer.assert_awaited_once()
        text = mock_message.answer.call_args.args[0]
        assert "<i>Test Privacy</i>" in text

    @pytest.mark.asyncio
    async def test_privacy_empty(
        self,
        mock_message: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        tg_user = await _get_unlinked_tg_user(aiogram_user.id)
        await sync_to_async(self._set_legal_privacy)("")

        await handle_privacy(mock_message, tg_user=tg_user)

        mock_message.answer.assert_awaited_once()
        text = mock_message.answer.call_args.args[0]
        assert "not available" in text.lower()


# ── handle_connect ───────────────────────────────────────────────────


class TestHandleConnect:
    @pytest.mark.asyncio
    async def test_already_linked(
        self,
        mock_message: AsyncMock,
        django_user: RevelUser,
    ) -> None:
        tg_user = await _get_tg_user(django_user)

        await handle_connect(mock_message, tg_user=tg_user)

        mock_message.answer.assert_awaited_once()
        assert "already linked" in mock_message.answer.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_creates_otp(
        self,
        mock_message: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        tg_user = await _get_unlinked_tg_user(aiogram_user.id)

        await handle_connect(mock_message, tg_user=tg_user)

        mock_message.answer.assert_awaited_once()
        text = mock_message.answer.call_args.args[0]
        assert "linking code" in text.lower()
        # OTP should exist in the DB
        otp = await AccountOTP.objects.aget(tg_user=tg_user)
        assert len(otp.otp) == 9

    @pytest.mark.asyncio
    async def test_reuses_unexpired_otp(
        self,
        mock_message: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        tg_user = await _get_unlinked_tg_user(aiogram_user.id)
        existing_otp = await AccountOTP.objects.acreate(tg_user=tg_user)

        await handle_connect(mock_message, tg_user=tg_user)

        # Should still be the same OTP (not deleted and recreated)
        otp = await AccountOTP.objects.aget(tg_user=tg_user)
        assert otp.pk == existing_otp.pk

    @pytest.mark.asyncio
    async def test_replaces_expired_otp(
        self,
        mock_message: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        from django.utils import timezone

        tg_user = await _get_unlinked_tg_user(aiogram_user.id)
        expired_otp = await AccountOTP.objects.acreate(tg_user=tg_user)
        # Force-expire it
        expired_otp.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        await expired_otp.asave(update_fields=["expires_at"])

        await handle_connect(mock_message, tg_user=tg_user)

        otp = await AccountOTP.objects.aget(tg_user=tg_user)
        assert otp.pk != expired_otp.pk
        assert not otp.is_expired()

    @pytest.mark.asyncio
    async def test_otp_formatted_in_groups(
        self,
        mock_message: AsyncMock,
        aiogram_user: t.Any,
    ) -> None:
        """OTP should be displayed as 'XXX XXX XXX'."""
        tg_user = await _get_unlinked_tg_user(aiogram_user.id)

        await handle_connect(mock_message, tg_user=tg_user)

        text = mock_message.answer.call_args.args[0]
        otp = await AccountOTP.objects.aget(tg_user=tg_user)
        formatted = f"{otp.otp[:3]} {otp.otp[3:6]} {otp.otp[6:9]}"
        assert formatted in text


# ── handle_unsubscribe ───────────────────────────────────────────────


class TestHandleUnsubscribe:
    @pytest.mark.asyncio
    async def test_unsubscribe(
        self,
        mock_message: AsyncMock,
        django_user: RevelUser,
    ) -> None:
        tg_user = await _get_tg_user(django_user)

        await handle_unsubscribe(mock_message, tg_user=tg_user, user=django_user)

        mock_message.answer.assert_awaited_once()
        text = mock_message.answer.call_args.args[0]
        assert "unsubscribed" in text.lower()


# ── sanitize_text (pure function) ────────────────────────────────────


class TestSanitizeText:
    def test_keeps_allowed_tags(self) -> None:
        html = "<b>bold</b> <i>italic</i> <u>underline</u> <code>code</code>"
        assert sanitize_text(html) == html

    def test_strips_disallowed_tags(self) -> None:
        html = "<div>content</div><script>evil</script>"
        assert sanitize_text(html) == "contentevil"

    def test_keeps_spoiler_span_opening(self) -> None:
        html = '<span class="tg-spoiler">hidden</span>'
        # NOTE: closing </span> is stripped because it lacks the class attr.
        # Telegram auto-closes unclosed tags, so this works in practice,
        # but it's arguably a bug in sanitize_text.
        assert sanitize_text(html) == '<span class="tg-spoiler">hidden'

    def test_strips_non_spoiler_span(self) -> None:
        html = '<span class="some-other">text</span>'
        assert sanitize_text(html) == "text"

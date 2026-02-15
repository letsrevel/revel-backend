# src/telegram/tests/test_handlers_admin.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from accounts.models import RevelUser
from telegram.fsm import BroadcastStates
from telegram.models import TelegramUser
from telegram.routers.admin import (
    cb_broadcast_confirm,
    handle_potential_broadcast_message,
)

pytestmark = pytest.mark.django_db


# ── helpers ──────────────────────────────────────────────────────────


async def _get_tg_user(user: RevelUser) -> TelegramUser:
    return await TelegramUser.objects.select_related("user").aget(user=user)


# ── handle_potential_broadcast_message ───────────────────────────────


class TestHandlePotentialBroadcastMessage:
    @pytest.mark.asyncio
    async def test_sets_confirming_state(
        self,
        mock_message: AsyncMock,
        mock_fsm_context: AsyncMock,
        django_superuser: RevelUser,
    ) -> None:
        tg_user = await _get_tg_user(django_superuser)
        mock_message.text = "Hello everyone!"
        mock_message.html_text = "<b>Hello everyone!</b>"

        await handle_potential_broadcast_message(
            mock_message, user=django_superuser, tg_user=tg_user, state=mock_fsm_context
        )

        mock_fsm_context.set_state.assert_awaited_once_with(BroadcastStates.confirming_broadcast)
        mock_fsm_context.update_data.assert_awaited_once_with(
            broadcast_message_text="Hello everyone!",
            broadcast_message_html_text="<b>Hello everyone!</b>",
        )
        mock_message.reply.assert_awaited_once()
        text = mock_message.reply.call_args.args[0]
        assert "BROADCAST" in text

    @pytest.mark.asyncio
    async def test_skipped_when_in_fsm_state(
        self,
        mock_message: AsyncMock,
        mock_fsm_context: AsyncMock,
        django_superuser: RevelUser,
    ) -> None:
        tg_user = await _get_tg_user(django_superuser)
        mock_message.text = "Hello everyone!"
        mock_fsm_context.get_state.return_value = "PreferenceStates:choosing_action"

        await handle_potential_broadcast_message(
            mock_message, user=django_superuser, tg_user=tg_user, state=mock_fsm_context
        )

        mock_fsm_context.set_state.assert_not_awaited()
        mock_message.reply.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skipped_for_empty_message(
        self,
        mock_message: AsyncMock,
        mock_fsm_context: AsyncMock,
        django_superuser: RevelUser,
    ) -> None:
        tg_user = await _get_tg_user(django_superuser)
        mock_message.text = "   "

        await handle_potential_broadcast_message(
            mock_message, user=django_superuser, tg_user=tg_user, state=mock_fsm_context
        )

        mock_fsm_context.set_state.assert_not_awaited()
        mock_message.reply.assert_not_awaited()


# ── cb_broadcast_confirm ─────────────────────────────────────────────


class TestBroadcastConfirm:
    @pytest.mark.asyncio
    async def test_confirm_yes_dispatches_task(
        self,
        mock_callback_query: AsyncMock,
        mock_fsm_context: AsyncMock,
        django_superuser: RevelUser,
    ) -> None:
        tg_user = await _get_tg_user(django_superuser)
        mock_callback_query.data = "broadcast_confirm:yes"
        mock_fsm_context.get_data.return_value = {
            "broadcast_message_text": "Hello!",
            "broadcast_message_html_text": "<b>Hello!</b>",
        }

        with patch("telegram.routers.admin.send_broadcast_message_task") as mock_task:
            mock_task.delay.return_value = MagicMock(id="task-123")

            await cb_broadcast_confirm(
                mock_callback_query, user=django_superuser, tg_user=tg_user, state=mock_fsm_context
            )

        mock_fsm_context.clear.assert_awaited_once()
        mock_task.delay.assert_called_once_with("<b>Hello!</b>")
        mock_callback_query.message.edit_text.assert_awaited_once()
        edit_text = mock_callback_query.message.edit_text.call_args.args[0]
        assert "initiated" in edit_text.lower()
        mock_callback_query.answer.assert_awaited_once()
        assert "task-123" in mock_callback_query.answer.call_args.args[0]

    @pytest.mark.asyncio
    async def test_confirm_no_cancels(
        self,
        mock_callback_query: AsyncMock,
        mock_fsm_context: AsyncMock,
        django_superuser: RevelUser,
    ) -> None:
        tg_user = await _get_tg_user(django_superuser)
        mock_callback_query.data = "broadcast_confirm:no"
        mock_fsm_context.get_data.return_value = {
            "broadcast_message_text": "Hello!",
            "broadcast_message_html_text": "<b>Hello!</b>",
        }

        with patch("telegram.routers.admin.send_broadcast_message_task") as mock_task:
            await cb_broadcast_confirm(
                mock_callback_query, user=django_superuser, tg_user=tg_user, state=mock_fsm_context
            )

        mock_fsm_context.clear.assert_awaited_once()
        mock_task.delay.assert_not_called()
        mock_callback_query.message.edit_text.assert_awaited_once()
        edit_text = mock_callback_query.message.edit_text.call_args.args[0]
        assert "cancelled" in edit_text.lower()
        mock_callback_query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confirm_yes_missing_content(
        self,
        mock_callback_query: AsyncMock,
        mock_fsm_context: AsyncMock,
        django_superuser: RevelUser,
    ) -> None:
        """When FSM data has no message content, show error."""
        tg_user = await _get_tg_user(django_superuser)
        mock_callback_query.data = "broadcast_confirm:yes"
        mock_fsm_context.get_data.return_value = {}

        with patch("telegram.routers.admin.send_broadcast_message_task") as mock_task:
            await cb_broadcast_confirm(
                mock_callback_query, user=django_superuser, tg_user=tg_user, state=mock_fsm_context
            )

        mock_task.delay.assert_not_called()
        mock_callback_query.answer.assert_awaited_once()
        assert "error" in mock_callback_query.answer.call_args.args[0].lower()

# src/telegram/tests/test_handlers_events.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Event, EventInvitationRequest, EventWaitList, Organization, WhitelistRequest
from events.service.event_manager import EventUserEligibility, NextStep, UserIsIneligibleError
from telegram.models import TelegramUser
from telegram.routers.events import (
    cb_handle_become_member,
    cb_handle_invitation_request_accept,
    cb_handle_invitation_request_reject,
    cb_handle_join_waitlist,
    cb_handle_request_invitation,
    cb_handle_rsvp,
    cb_handle_whitelist_request_approve,
    cb_handle_whitelist_request_reject,
)

pytestmark = pytest.mark.django_db


# ── helpers ──────────────────────────────────────────────────────────


async def _get_tg_user(user: RevelUser) -> TelegramUser:
    return await TelegramUser.objects.select_related("user").aget(user=user)


# ── cb_handle_rsvp ──────────────────────────────────────────────────


class TestHandleRsvp:
    @pytest.mark.asyncio
    async def test_rsvp_yes(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"rsvp:{private_event.id}:yes"

        with patch("telegram.routers.events.get_ticket_handler", new_callable=AsyncMock) as mock_get_handler:
            mock_handler = MagicMock()
            mock_handler.rsvp = MagicMock()
            mock_get_handler.return_value = mock_handler

            await cb_handle_rsvp(mock_callback_query, user=django_user, tg_user=tg_user)

        mock_callback_query.message.answer.assert_awaited_once()
        text = mock_callback_query.message.answer.call_args.args[0]
        assert "confirming your presence" in text
        mock_callback_query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rsvp_no(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"rsvp:{private_event.id}:no"

        with patch("telegram.routers.events.get_ticket_handler", new_callable=AsyncMock) as mock_get_handler:
            mock_handler = MagicMock()
            mock_handler.rsvp = MagicMock()
            mock_get_handler.return_value = mock_handler

            await cb_handle_rsvp(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "No hard feelings" in text

    @pytest.mark.asyncio
    async def test_rsvp_maybe(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"rsvp:{private_event.id}:maybe"

        with patch("telegram.routers.events.get_ticket_handler", new_callable=AsyncMock) as mock_get_handler:
            mock_handler = MagicMock()
            mock_handler.rsvp = MagicMock()
            mock_get_handler.return_value = mock_handler

            await cb_handle_rsvp(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "let us know at a later time" in text

    @pytest.mark.asyncio
    async def test_rsvp_ineligible_join_waitlist(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        """When user is ineligible with JOIN_WAITLIST next step, show waitlist keyboard."""
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"rsvp:{private_event.id}:yes"

        eligibility = EventUserEligibility(
            allowed=False,
            event_id=private_event.id,
            reason="Event is full",
            next_step=NextStep.JOIN_WAITLIST,
        )
        error = UserIsIneligibleError("Ineligible", eligibility)

        with (
            patch("telegram.routers.events.get_ticket_handler", new_callable=AsyncMock) as mock_get_handler,
            patch("telegram.routers.events.get_event_eligible_keyboard") as mock_keyboard,
        ):
            mock_handler = MagicMock()
            mock_handler.rsvp = MagicMock(side_effect=error)
            mock_get_handler.return_value = mock_handler
            mock_keyboard.return_value = MagicMock()

            await cb_handle_rsvp(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "currently full" in text
        assert "waitlist" in text.lower()
        mock_keyboard.assert_called_once()
        mock_callback_query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rsvp_ineligible_generic(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        """When user is ineligible with a non-waitlist reason, show generic error."""
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"rsvp:{private_event.id}:yes"

        eligibility = EventUserEligibility(
            allowed=False,
            event_id=private_event.id,
            reason="You must be a member",
            next_step=NextStep.BECOME_MEMBER,
        )
        error = UserIsIneligibleError("Ineligible", eligibility)

        with patch("telegram.routers.events.get_ticket_handler", new_callable=AsyncMock) as mock_get_handler:
            mock_handler = MagicMock()
            mock_handler.rsvp = MagicMock(side_effect=error)
            mock_get_handler.return_value = mock_handler

            await cb_handle_rsvp(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "not eligible" in text
        assert "You must be a member" in text
        mock_callback_query.answer.assert_awaited_once()


# ── cb_handle_request_invitation ─────────────────────────────────────


class TestHandleRequestInvitation:
    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"request_invitation:{private_event.id}"

        with patch("telegram.routers.events.event_service") as mock_svc:
            mock_svc.create_invitation_request = MagicMock()

            await cb_handle_request_invitation(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "invitation request" in text.lower()
        assert private_event.name in text

    @pytest.mark.asyncio
    async def test_service_failure(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"request_invitation:{private_event.id}"

        with patch("telegram.routers.events.event_service") as mock_svc:
            mock_svc.create_invitation_request = MagicMock(side_effect=Exception("DB error"))

            await cb_handle_request_invitation(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "something went wrong" in text.lower()


# ── cb_handle_become_member ──────────────────────────────────────────


class TestHandleBecomeMember:
    @pytest.mark.asyncio
    async def test_success(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        organization: Organization,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"become_member:{organization.id}"

        with patch("telegram.routers.events.organization_service") as mock_svc:
            mock_svc.create_membership_request = MagicMock()

            await cb_handle_become_member(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "membership request" in text.lower()
        assert organization.name in text

    @pytest.mark.asyncio
    async def test_service_failure(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        organization: Organization,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        mock_callback_query.data = f"become_member:{organization.id}"

        with patch("telegram.routers.events.organization_service") as mock_svc:
            mock_svc.create_membership_request = MagicMock(side_effect=Exception("Nope"))

            await cb_handle_become_member(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "something went wrong" in text.lower()


# ── cb_handle_join_waitlist ──────────────────────────────────────────


class TestHandleJoinWaitlist:
    @pytest.mark.asyncio
    async def test_join_success(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        # Open the waitlist
        private_event.waitlist_open = True
        await private_event.asave(update_fields=["waitlist_open"])

        mock_callback_query.data = f"join_waitlist:{private_event.id}"

        await cb_handle_join_waitlist(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "on the waitlist" in text.lower()
        assert await EventWaitList.objects.filter(event=private_event, user=django_user).aexists()
        mock_callback_query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_already_on_waitlist(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        private_event.waitlist_open = True
        await private_event.asave(update_fields=["waitlist_open"])
        await EventWaitList.objects.acreate(event=private_event, user=django_user)

        mock_callback_query.data = f"join_waitlist:{private_event.id}"

        await cb_handle_join_waitlist(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "already on the waitlist" in text.lower()
        mock_callback_query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_waitlist_not_open(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user = await _get_tg_user(django_user)
        # waitlist_open defaults to False
        mock_callback_query.data = f"join_waitlist:{private_event.id}"

        await cb_handle_join_waitlist(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "not currently open" in text.lower()
        mock_callback_query.answer.assert_awaited_once()


# ── cb_handle_invitation_request_accept ──────────────────────────────


class TestHandleInvitationRequestAccept:
    @pytest.mark.asyncio
    async def test_accept_success(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        django_superuser: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user_superuser = await _get_tg_user(django_superuser)
        request = await EventInvitationRequest.objects.acreate(event=private_event, user=django_user)
        # Pre-fetch related objects for display_name access
        request = await EventInvitationRequest.objects.select_related("user", "event").aget(pk=request.pk)

        mock_callback_query.data = f"invitation_request_accept:{request.pk}"

        with patch("telegram.routers.events.event_service") as mock_svc:
            mock_svc.approve_invitation_request = MagicMock()

            await cb_handle_invitation_request_accept(
                mock_callback_query, user=django_superuser, tg_user=tg_user_superuser, checked_entity=request
            )

        mock_callback_query.message.edit_text.assert_awaited_once()
        text = mock_callback_query.message.edit_text.call_args.args[0]
        assert "accepted" in text.lower()
        mock_svc.approve_invitation_request.assert_called_once_with(request, decided_by=django_superuser)

    @pytest.mark.asyncio
    async def test_accept_service_failure(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        django_superuser: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user_superuser = await _get_tg_user(django_superuser)
        request = await EventInvitationRequest.objects.acreate(event=private_event, user=django_user)
        request = await EventInvitationRequest.objects.select_related("user", "event").aget(pk=request.pk)

        with patch("telegram.routers.events.event_service") as mock_svc:
            mock_svc.approve_invitation_request = MagicMock(side_effect=Exception("fail"))

            await cb_handle_invitation_request_accept(
                mock_callback_query, user=django_superuser, tg_user=tg_user_superuser, checked_entity=request
            )

        mock_callback_query.answer.assert_awaited_once()
        call_kwargs = mock_callback_query.answer.call_args
        assert "something went wrong" in call_kwargs.args[0].lower()
        assert call_kwargs.kwargs["show_alert"] is True


# ── cb_handle_invitation_request_reject ──────────────────────────────


class TestHandleInvitationRequestReject:
    @pytest.mark.asyncio
    async def test_reject_success(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        django_superuser: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user_superuser = await _get_tg_user(django_superuser)
        request = await EventInvitationRequest.objects.acreate(event=private_event, user=django_user)
        request = await EventInvitationRequest.objects.select_related("user", "event").aget(pk=request.pk)

        with patch("telegram.routers.events.event_service") as mock_svc:
            mock_svc.reject_invitation_request = MagicMock()

            await cb_handle_invitation_request_reject(
                mock_callback_query, user=django_superuser, tg_user=tg_user_superuser, checked_entity=request
            )

        mock_callback_query.message.edit_text.assert_awaited_once()
        text = mock_callback_query.message.edit_text.call_args.args[0]
        assert "rejected" in text.lower()
        mock_svc.reject_invitation_request.assert_called_once_with(request, decided_by=django_superuser)

    @pytest.mark.asyncio
    async def test_reject_service_failure(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        django_superuser: RevelUser,
        private_event: Event,
    ) -> None:
        tg_user_superuser = await _get_tg_user(django_superuser)
        request = await EventInvitationRequest.objects.acreate(event=private_event, user=django_user)
        request = await EventInvitationRequest.objects.select_related("user", "event").aget(pk=request.pk)

        with patch("telegram.routers.events.event_service") as mock_svc:
            mock_svc.reject_invitation_request = MagicMock(side_effect=Exception("fail"))

            await cb_handle_invitation_request_reject(
                mock_callback_query, user=django_superuser, tg_user=tg_user_superuser, checked_entity=request
            )

        mock_callback_query.answer.assert_awaited_once()
        assert "something went wrong" in mock_callback_query.answer.call_args.args[0].lower()


# ── cb_handle_whitelist_request_approve ──────────────────────────────


class TestHandleWhitelistRequestApprove:
    @pytest.mark.asyncio
    async def test_approve_success(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        django_superuser: RevelUser,
        organization: Organization,
    ) -> None:
        tg_user_superuser = await _get_tg_user(django_superuser)
        request = await WhitelistRequest.objects.acreate(organization=organization, user=django_user)
        request = await WhitelistRequest.objects.select_related("user", "organization").aget(pk=request.pk)

        with patch("telegram.routers.events.whitelist_service") as mock_svc:
            mock_svc.approve_whitelist_request = MagicMock()

            await cb_handle_whitelist_request_approve(
                mock_callback_query, user=django_superuser, tg_user=tg_user_superuser, checked_entity=request
            )

        mock_callback_query.message.edit_text.assert_awaited_once()
        text = mock_callback_query.message.edit_text.call_args.args[0]
        assert "approved" in text.lower()
        mock_svc.approve_whitelist_request.assert_called_once_with(request, decided_by=django_superuser)

    @pytest.mark.asyncio
    async def test_approve_service_failure(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        django_superuser: RevelUser,
        organization: Organization,
    ) -> None:
        tg_user_superuser = await _get_tg_user(django_superuser)
        request = await WhitelistRequest.objects.acreate(organization=organization, user=django_user)
        request = await WhitelistRequest.objects.select_related("user", "organization").aget(pk=request.pk)

        with patch("telegram.routers.events.whitelist_service") as mock_svc:
            mock_svc.approve_whitelist_request = MagicMock(side_effect=Exception("boom"))

            await cb_handle_whitelist_request_approve(
                mock_callback_query, user=django_superuser, tg_user=tg_user_superuser, checked_entity=request
            )

        mock_callback_query.answer.assert_awaited_once()
        assert "something went wrong" in mock_callback_query.answer.call_args.args[0].lower()


# ── cb_handle_whitelist_request_reject ───────────────────────────────


class TestHandleWhitelistRequestReject:
    @pytest.mark.asyncio
    async def test_reject_success(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        django_superuser: RevelUser,
        organization: Organization,
    ) -> None:
        tg_user_superuser = await _get_tg_user(django_superuser)
        request = await WhitelistRequest.objects.acreate(organization=organization, user=django_user)
        request = await WhitelistRequest.objects.select_related("user", "organization").aget(pk=request.pk)

        with patch("telegram.routers.events.whitelist_service") as mock_svc:
            mock_svc.reject_whitelist_request = MagicMock()

            await cb_handle_whitelist_request_reject(
                mock_callback_query, user=django_superuser, tg_user=tg_user_superuser, checked_entity=request
            )

        mock_callback_query.message.edit_text.assert_awaited_once()
        text = mock_callback_query.message.edit_text.call_args.args[0]
        assert "rejected" in text.lower()
        mock_svc.reject_whitelist_request.assert_called_once_with(request, decided_by=django_superuser)

    @pytest.mark.asyncio
    async def test_reject_service_failure(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        django_superuser: RevelUser,
        organization: Organization,
    ) -> None:
        tg_user_superuser = await _get_tg_user(django_superuser)
        request = await WhitelistRequest.objects.acreate(organization=organization, user=django_user)
        request = await WhitelistRequest.objects.select_related("user", "organization").aget(pk=request.pk)

        with patch("telegram.routers.events.whitelist_service") as mock_svc:
            mock_svc.reject_whitelist_request = MagicMock(side_effect=Exception("boom"))

            await cb_handle_whitelist_request_reject(
                mock_callback_query, user=django_superuser, tg_user=tg_user_superuser, checked_entity=request
            )

        mock_callback_query.answer.assert_awaited_once()
        assert "something went wrong" in mock_callback_query.answer.call_args.args[0].lower()

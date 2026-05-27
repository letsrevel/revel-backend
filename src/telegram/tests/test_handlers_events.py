# src/telegram/tests/test_handlers_events.py
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from accounts.models import RevelUser
from events.models import Event, EventInvitationRequest, EventRSVP, EventWaitList, Organization, WhitelistRequest
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


async def _make_event_visible(event: Event, *, waitlist_open: bool) -> None:
    """Make the event publicly visible (PUBLIC + OPEN) so a plain user can resolve it."""
    event.visibility = Event.Visibility.PUBLIC
    event.status = Event.EventStatus.OPEN
    event.waitlist_open = waitlist_open
    await event.asave(update_fields=["visibility", "status", "waitlist_open"])


async def _make_event_visible_and_full(event: Event, filler: RevelUser) -> None:
    """Make the event visible, RSVP-based, and at capacity so eligibility yields JOIN_WAITLIST."""
    event.visibility = Event.Visibility.PUBLIC
    event.status = Event.EventStatus.OPEN
    event.requires_ticket = False
    event.waitlist_open = True
    event.max_attendees = 1
    await event.asave(update_fields=["visibility", "status", "requires_ticket", "waitlist_open", "max_attendees"])
    await EventRSVP.objects.acreate(event=event, user=filler, status=EventRSVP.RsvpStatus.YES)


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
        """A user eligible-except-for-capacity on a visible, full event joins the waitlist."""
        tg_user = await _get_tg_user(django_user)
        filler = await RevelUser.objects.acreate(username="wl_filler", password="<PASSWORD>")
        await _make_event_visible_and_full(private_event, filler)

        mock_callback_query.data = f"join_waitlist:{private_event.id}"

        with patch("telegram.routers.events.enqueue_waitlist_processing"):
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
        await _make_event_visible(private_event, waitlist_open=True)
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
        await _make_event_visible(private_event, waitlist_open=False)

        mock_callback_query.data = f"join_waitlist:{private_event.id}"

        await cb_handle_join_waitlist(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "not currently open" in text.lower()
        mock_callback_query.answer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invisible_event_is_not_joinable(
        self,
        mock_callback_query: AsyncMock,
        django_user: RevelUser,
        private_event: Event,
    ) -> None:
        """A user who cannot see the event must not be able to waitlist it by ID.

        Guards the security fix: the bot resolves via Event.objects.for_user(), so a
        PRIVATE event the user is neither invited to nor a member/staff of is rejected
        before any waitlist row is created (no seat-squatting on hidden events).
        """
        tg_user = await _get_tg_user(django_user)
        # private_event stays PRIVATE/DRAFT; django_user has no invitation/membership.
        private_event.waitlist_open = True
        await private_event.asave(update_fields=["waitlist_open"])

        mock_callback_query.data = f"join_waitlist:{private_event.id}"

        await cb_handle_join_waitlist(mock_callback_query, user=django_user, tg_user=tg_user)

        text = mock_callback_query.message.answer.call_args.args[0]
        assert "not available" in text.lower()
        assert not await EventWaitList.objects.filter(event=private_event, user=django_user).aexists()
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

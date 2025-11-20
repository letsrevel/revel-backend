# src/telegram/routers/events.py

import typing as t
import uuid

import structlog
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from asgiref.sync import sync_to_async

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitationRequest,
    EventWaitList,
    Organization,
)
from events.service import event_service, organization_service
from events.service.event_manager import EventManager, NextStep, UserIsIneligibleError
from telegram.keyboards import get_event_eligible_keyboard
from telegram.middleware import AuthorizationMiddleware
from telegram.models import TelegramUser

logger = structlog.get_logger(__name__)
router = Router(name="events-router")

# Register middleware at router level to access handler flags
router.message.middleware(AuthorizationMiddleware())
router.callback_query.middleware(AuthorizationMiddleware())


@sync_to_async
def get_ticket_handler(user: RevelUser, event: Event) -> EventManager:
    """Helper function to get TicketHandler async."""
    return EventManager(user, event)


@router.callback_query(F.data.startswith("rsvp:"), flags={"requires_linked_user": True})
async def cb_handle_rsvp(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles RSVP callback queries."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)

    _, event_id_str, status = callback.data.split(":")
    event_id = uuid.UUID(event_id_str)
    event = await Event.objects.aget(id=event_id)

    handler = await get_ticket_handler(user, event)

    try:
        await sync_to_async(handler.rsvp)(status)  # type: ignore[arg-type]
    except UserIsIneligibleError as e:
        logger.warning(
            "user_ineligible_for_rsvp",
            username=user.username,
            event_id=str(event.id),
            reason=e.eligibility.reason,
            next_step=e.eligibility.next_step,
        )

        # Handle JOIN_WAITLIST case with appropriate keyboard
        if e.eligibility.next_step == NextStep.JOIN_WAITLIST:
            keyboard = await sync_to_async(get_event_eligible_keyboard)(event, e.eligibility, user)
            await callback.message.answer(
                f"⌛ Sorry, <b>{event.name}</b> is currently full.\n\n"
                f"{e.eligibility.reason}\n\n"
                f"You can join the waitlist to be notified when spots become available.",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            await callback.answer()
            return

        # For other ineligibility cases, show a generic message
        await callback.message.answer(
            f"❌ Sorry, you are not eligible to RSVP for <b>{event.name}</b>.\n\nReason: {e.eligibility.reason}",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    # Only send success message if RSVP actually succeeded
    await callback.message.answer(_get_rsvp_response_text(status).format(event.name))  # type: ignore[arg-type]
    await callback.answer()


def _get_rsvp_response_text(rsvp: t.Literal["yes", "no", "maybe"]) -> str:
    match rsvp:
        case "yes":
            return 'Thank you for confirming your presence to "{}". See you there 🎉'
        case "no":
            return "No hard feelings ✌️"
        case "maybe":
            return "No worries, let us know at a later time 😌"
    raise ValueError(f"Invalid rsvp: {rsvp}")


@router.callback_query(F.data.startswith("request_invitation:"), flags={"requires_linked_user": True})
async def cb_handle_request_invitation(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles the 'Request Invitation' button press."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, event_id_str = callback.data.split(":")
    event_id = uuid.UUID(event_id_str)
    event = await Event.objects.select_related("organization").aget(id=event_id)

    # TODO: Implement FSM state for prompting custom message
    # For now, create invitation request without message

    try:
        await sync_to_async(event_service.create_invitation_request)(event=event, user=user, message=None)
        await callback.message.answer(
            f"✅ Your invitation request for <b>{event.name}</b> has been sent to the organizers. "
            f"You'll be notified when they respond."
        )
    except Exception as e:
        logger.exception(
            "failed_to_create_invitation_request", event_id=str(event.id), user_id=str(user.id), error=str(e)
        )
        await callback.message.answer("❌ Sorry, something went wrong. Please try again later.")


@router.callback_query(F.data.startswith("become_member:"), flags={"requires_linked_user": True})
async def cb_handle_become_member(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles the 'Request Membership' button press."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, org_id_str = callback.data.split(":")
    org_id = uuid.UUID(org_id_str)

    try:
        organization = await Organization.objects.aget(id=org_id)
        await sync_to_async(organization_service.create_membership_request)(
            organization=organization, user=user, message=None
        )
        await callback.message.answer(
            f"✅ Your membership request for <b>{organization.name}</b> has been sent to the organizers. "
            f"You'll be notified when they respond."
        )
    except Exception as e:
        logger.exception("failed_to_create_membership_request", org_id=str(org_id), user_id=str(user.id), error=str(e))
        await callback.message.answer("❌ Sorry, something went wrong. Please try again later.")


@router.callback_query(F.data.startswith("join_waitlist:"), flags={"requires_linked_user": True})
async def cb_handle_join_waitlist(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles the 'Join Waitlist' button press."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, event_id_str = callback.data.split(":")
    event_id = uuid.UUID(event_id_str)

    try:
        event = await Event.objects.aget(id=event_id)

        # Check if waitlist is open (mirror validation from /waitlist/join endpoint)
        if not event.waitlist_open:
            await callback.message.answer(
                f"❌ The waitlist for <b>{event.name}</b> is not currently open.",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        _, created = await EventWaitList.objects.aget_or_create(event_id=event_id, user=user)

        if created:
            await callback.message.answer(f"✅ You are on the waitlist for <b>{event.name}</b>!", parse_mode="HTML")
        else:
            await callback.message.answer(
                f"ℹ️ You are already on the waitlist for <b>{event.name}</b>!", parse_mode="HTML"
            )
        await callback.answer()
    except Exception as e:
        logger.exception("failed_to_join_waitlist", event_id=str(event_id), user_id=str(user.id), error=str(e))
        await callback.message.answer("❌ Sorry, something went wrong. Please try again later.")
        await callback.answer()


@router.callback_query(F.data.startswith("invitation_request_accept:"), flags={"requires_linked_user": True})
async def cb_handle_invitation_request_accept(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles accepting an invitation request (organizer action)."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, request_id_str = callback.data.split(":")
    request_id = uuid.UUID(request_id_str)

    try:
        request = await EventInvitationRequest.objects.select_related("event", "user").aget(pk=request_id)
        await sync_to_async(event_service.approve_invitation_request)(request, decided_by=user)
        await callback.message.edit_text(
            f"✅ Invitation request from <b>{request.user.get_display_name()}</b> "
            f"for <b>{request.event.name}</b> has been <b>accepted</b>.",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception(
            "failed_to_accept_invitation_request", request_id=str(request_id), user_id=str(user.id), error=str(e)
        )
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)


@router.callback_query(F.data.startswith("invitation_request_reject:"), flags={"requires_linked_user": True})
async def cb_handle_invitation_request_reject(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles rejecting an invitation request (organizer action)."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, request_id_str = callback.data.split(":")
    request_id = uuid.UUID(request_id_str)

    try:
        request = await EventInvitationRequest.objects.select_related("event", "user").aget(pk=request_id)
        await sync_to_async(event_service.reject_invitation_request)(request, decided_by=user)
        await callback.message.edit_text(
            f"❌ Invitation request from <b>{request.user.get_display_name()}</b> "
            f"for <b>{request.event.name}</b> has been <b>rejected</b>.",
            reply_markup=None,
            parse_mode="HTML",
        )
    except EventInvitationRequest.DoesNotExist:
        await callback.answer("❌ Invitation request not found.", show_alert=True)
    except Exception as e:
        logger.exception(
            "failed_to_reject_invitation_request", request_id=str(request_id), user_id=str(user.id), error=str(e)
        )
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)

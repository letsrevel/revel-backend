# src/telegram/routers/events.py

import logging
import typing as t
import uuid

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from asgiref.sync import sync_to_async

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitationRequest,
    EventWaitList,
    Organization,
    OrganizationMembershipRequest,
)
from events.service import event_service, organization_service
from events.service.event_manager import EventManager, UserIsIneligibleError
from telegram.middleware import AuthorizationMiddleware
from telegram.models import TelegramUser

logger = logging.getLogger(__name__)
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
        logger.warning(f"User {user.username} was ineligible for RSVP to event {event.id}: {e.eligibility.reason}")
        await callback.answer(
            f"Sorry, you are not eligible to RSVP (yet). Reason: {e.eligibility.reason}", show_alert=False
        )

    await callback.message.answer(_get_rsvp_response_text(status).format(event.name))  # type: ignore[arg-type]


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
        logger.exception(f"Failed to create invitation request: {e}")
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
        logger.exception(f"Failed to create membership request: {e}")
        await callback.message.answer("❌ Sorry, something went wrong. Please try again later.")


@router.callback_query(F.data.startswith("join_waitlist:"), flags={"requires_linked_user": True})
async def cb_handle_join_waitlist(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles the 'Request Membership' button press."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, event_id_str = callback.data.split(":")
    event_id = uuid.UUID(event_id_str)

    try:
        event = await Event.objects.aget(id=event_id)
        await EventWaitList.objects.aget_or_create(event_id=event_id, user=user)

        await callback.message.answer(f"✅ You are on the waitlist for {event.name}!")
    except Exception as e:
        logger.exception(f"Failed to join waitlist: {e}")
        await callback.message.answer("❌ Sorry, something went wrong. Please try again later.")


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
        logger.exception(f"Failed to accept invitation request: {e}")
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
        logger.exception(f"Failed to reject invitation request: {e}")
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)


@router.callback_query(F.data.startswith("membership_request_approve:"), flags={"requires_linked_user": True})
async def cb_handle_membership_request_approve(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles approving a membership request (organizer action)."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, request_id_str = callback.data.split(":")
    request_id = uuid.UUID(request_id_str)

    try:
        request = await OrganizationMembershipRequest.objects.select_related("organization", "user").aget(pk=request_id)
        await sync_to_async(organization_service.approve_membership_request)(request, decided_by=user)
        await callback.message.edit_text(
            f"✅ Membership request from <b>{request.user.get_display_name()}</b> "
            f"for <b>{request.organization.name}</b> has been <b>approved</b>.",
            reply_markup=None,
            parse_mode="HTML",
        )
    except OrganizationMembershipRequest.DoesNotExist:
        await callback.answer("❌ Membership request not found.", show_alert=True)
    except Exception as e:
        logger.exception(f"Failed to approve membership request: {e}")
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)


@router.callback_query(F.data.startswith("membership_request_reject:"), flags={"requires_linked_user": True})
async def cb_handle_membership_request_reject(callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser) -> None:
    """Handles rejecting a membership request (organizer action)."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, request_id_str = callback.data.split(":")
    request_id = uuid.UUID(request_id_str)

    try:
        request = await OrganizationMembershipRequest.objects.select_related("organization", "user").aget(pk=request_id)
        await sync_to_async(organization_service.reject_membership_request)(request, decided_by=user)
        await callback.message.edit_text(
            f"❌ Membership request from <b>{request.user.get_display_name()}</b> "
            f"for <b>{request.organization.name}</b> has been <b>rejected</b>.",
            reply_markup=None,
            parse_mode="HTML",
        )
    except OrganizationMembershipRequest.DoesNotExist:
        await callback.answer("❌ Membership request not found.", show_alert=True)
    except Exception as e:
        logger.exception(f"Failed to reject membership request: {e}")
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)


# TODO: handlers for questionnaire and other NextSteps when appropriate

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
    WhitelistRequest,
)
from events.service import event_service, organization_service, whitelist_service
from events.service.event_manager import EligibilityService, EventManager, NextStep, UserIsIneligibleError
from events.service.waitlist_service import enqueue_waitlist_processing
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
        # Resolve within the user's visibility scope so the bot never acts on (or reveals)
        # an event the user cannot see — mirrors the HTTP endpoint's get_one().
        try:
            event = await Event.objects.for_user(user).aget(id=event_id)
        except Event.DoesNotExist:
            await callback.message.answer("❌ This event is not available.", parse_mode="HTML")
            await callback.answer()
            return

        if not event.waitlist_open:
            await callback.message.answer(
                f"❌ The waitlist for <b>{event.name}</b> is not currently open.",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        # Idempotency: already on the waitlist.
        if await EventWaitList.objects.filter(event=event, user=user).aexists():
            await callback.message.answer(
                f"ℹ️ You are already on the waitlist for <b>{event.name}</b>!", parse_mode="HTML"
            )
            await callback.answer()
            return

        # Run the full eligibility pipeline and only join when capacity is the *sole* obstacle,
        # exactly like the HTTP /waitlist/join endpoint. Without this, an ineligible user could
        # join the waitlist and squat scarce capacity via PENDING offers (and waitlist invisible
        # events), since waitlist selection trusts membership without re-checking eligibility.
        eligibility = await sync_to_async(lambda: EligibilityService(user, event).check_eligibility())()
        if eligibility.allowed:
            await callback.message.answer(
                f"✅ <b>{event.name}</b> has spots available — you can register directly!",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        if eligibility.next_step not in {NextStep.JOIN_WAITLIST, NextStep.WAIT_FOR_OPEN_SPOT}:
            await callback.message.answer(
                f"❌ Sorry, you are not eligible to join the waitlist for <b>{event.name}</b>.\n\n"
                f"Reason: {eligibility.reason}",
                parse_mode="HTML",
            )
            await callback.answer()
            return

        _, created = await EventWaitList.objects.aget_or_create(event=event, user=user)
        await sync_to_async(enqueue_waitlist_processing)(event.id)

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


@router.callback_query(
    F.data.startswith("invitation_request_accept:"),
    flags={
        "requires_linked_user": True,
        "staff_permission": "invite_to_event",
        "permission_entity": "invitation_request",
    },
)
async def cb_handle_invitation_request_accept(
    callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser, checked_entity: EventInvitationRequest
) -> None:
    """Handles accepting an invitation request (organizer action)."""
    assert isinstance(callback.message, Message)

    try:
        await sync_to_async(event_service.approve_invitation_request)(checked_entity, decided_by=user)
        await callback.message.edit_text(
            f"✅ Invitation request from <b>{checked_entity.user.get_display_name()}</b> "
            f"for <b>{checked_entity.event.name}</b> has been <b>accepted</b>.",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception(
            "failed_to_accept_invitation_request",
            request_id=str(checked_entity.pk),
            user_id=str(user.id),
            error=str(e),
        )
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)


@router.callback_query(
    F.data.startswith("invitation_request_reject:"),
    flags={
        "requires_linked_user": True,
        "staff_permission": "invite_to_event",
        "permission_entity": "invitation_request",
    },
)
async def cb_handle_invitation_request_reject(
    callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser, checked_entity: EventInvitationRequest
) -> None:
    """Handles rejecting an invitation request (organizer action)."""
    assert isinstance(callback.message, Message)

    try:
        await sync_to_async(event_service.reject_invitation_request)(checked_entity, decided_by=user)
        await callback.message.edit_text(
            f"❌ Invitation request from <b>{checked_entity.user.get_display_name()}</b> "
            f"for <b>{checked_entity.event.name}</b> has been <b>rejected</b>.",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception(
            "failed_to_reject_invitation_request",
            request_id=str(checked_entity.pk),
            user_id=str(user.id),
            error=str(e),
        )
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)


@router.callback_query(
    F.data.startswith("whitelist_request_approve:"),
    flags={
        "requires_linked_user": True,
        "staff_permission": "manage_members",
        "permission_entity": "whitelist_request",
    },
)
async def cb_handle_whitelist_request_approve(
    callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser, checked_entity: WhitelistRequest
) -> None:
    """Handles approving a whitelist request (organizer action)."""
    assert isinstance(callback.message, Message)

    try:
        await sync_to_async(whitelist_service.approve_whitelist_request)(checked_entity, decided_by=user)
        await callback.message.edit_text(
            f"✅ Whitelist request from <b>{checked_entity.user.get_display_name()}</b> "
            f"for <b>{checked_entity.organization.name}</b> has been <b>approved</b>.",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception(
            "failed_to_approve_whitelist_request",
            request_id=str(checked_entity.pk),
            user_id=str(user.id),
            error=str(e),
        )
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)


@router.callback_query(
    F.data.startswith("whitelist_request_reject:"),
    flags={
        "requires_linked_user": True,
        "staff_permission": "manage_members",
        "permission_entity": "whitelist_request",
    },
)
async def cb_handle_whitelist_request_reject(
    callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser, checked_entity: WhitelistRequest
) -> None:
    """Handles rejecting a whitelist request (organizer action)."""
    assert isinstance(callback.message, Message)

    try:
        await sync_to_async(whitelist_service.reject_whitelist_request)(checked_entity, decided_by=user)
        await callback.message.edit_text(
            f"❌ Whitelist request from <b>{checked_entity.user.get_display_name()}</b> "
            f"for <b>{checked_entity.organization.name}</b> has been <b>rejected</b>.",
            reply_markup=None,
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception(
            "failed_to_reject_whitelist_request",
            request_id=str(checked_entity.pk),
            user_id=str(user.id),
            error=str(e),
        )
        await callback.answer("❌ Sorry, something went wrong. Please try again later.", show_alert=True)

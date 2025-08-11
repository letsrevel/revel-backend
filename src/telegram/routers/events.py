# src/telegram/routers/events.py

import logging
import typing as t
import uuid

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from asgiref.sync import sync_to_async

from accounts.models import RevelUser
from events.models import Event, EventWaitList, TicketTier
from events.service.event_manager import EventManager, NextStep, UserIsIneligibleError
from telegram.keyboards import get_event_eligible_keyboard
from telegram.utils import generate_qr_code

logger = logging.getLogger(__name__)
router = Router(name="events-router")


@sync_to_async
def get_ticket_handler(user: RevelUser, event: Event) -> EventManager:
    """Helper function to get TicketHandler async."""
    return EventManager(user, event)


@router.callback_query(F.data.startswith("rsvp:"))
async def cb_handle_rsvp(callback: CallbackQuery, user: RevelUser) -> None:
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


@router.callback_query(F.data.startswith("get_ticket:"))
async def cb_get_ticket(callback: CallbackQuery, user: RevelUser) -> None:
    """Handles the 'Get Ticket' button press."""
    assert callback.data is not None
    _, tier_id_str = callback.data.split(":")
    tier_id = uuid.UUID(tier_id_str)
    tier = await TicketTier.objects.aget(id=tier_id)
    event = await Event.objects.aget(id=tier.event_id)

    handler = await get_ticket_handler(user, event)
    try:
        ticket = await sync_to_async(handler.create_ticket)(tier)

        if isinstance(ticket, str):
            await callback.answer(f"Click on the url to complete the purchase: {ticket}")
            return

        await callback.answer("Your ticket is being generated...")
        assert isinstance(callback.message, Message)
        qr_code_input_file = await sync_to_async(generate_qr_code)(str(ticket.id))
        await callback.message.answer_photo(
            photo=qr_code_input_file,
            caption=(
                f"🎟️ Here is your ticket for **{event.name}**!\n\n"
                f"Present this QR code at the entrance for check-in. "
                f"See you there!"
            ),
        )
        if isinstance(callback.message, Message):
            await callback.message.delete()  # Clean up the initial button
    except UserIsIneligibleError as e:
        logger.warning(f"User {user.username} was ineligible for ticket to event {event.id}: {e.eligibility.reason}")
        await callback.answer(
            f"Sorry, you are not eligible for a ticket (yet). Reason: {e.eligibility.reason}", show_alert=False
        )


@router.callback_query(F.data.startswith("join_waitlist:"))
async def cb_handle_join_waitlist(callback: CallbackQuery, user: RevelUser) -> None:
    """Handles the 'Join Waitlist' button press."""
    assert callback.data is not None
    assert isinstance(callback.message, Message)
    _, event_id_str = callback.data.split(":")
    event_id = uuid.UUID(event_id_str)
    event = await Event.objects.prefetch_related("ticket_tiers").aget(id=event_id)
    handler = await get_ticket_handler(user, event)
    eligibility = await sync_to_async(handler.check_eligibility)()
    if eligibility.allowed:  # the party opened up meanwhile!
        reply_markup = get_event_eligible_keyboard(event, eligibility, user)
        await callback.message.answer(text="Good news, the Event opened up!", reply_markup=reply_markup)
        return
    if eligibility.next_step == NextStep.JOIN_WAITLIST:
        await EventWaitList.objects.aget_or_create(event=event, user=user)
        await callback.message.answer("Congrats! You are in the waiting list ⏳")
        return
    await callback.message.answer(
        f"Ooops. Something went wrong. You cannot join the waiting list. Reason: {eligibility.reason}"
    )


# TODO: handlers for other NextSteps when appropriate

# src/telegram/keyboards.py


from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event
from events.service.event_manager import EventUserEligibility, NextStep

# --- Reply Keyboards ---

remove_keyboard = ReplyKeyboardRemove()  # Convenience object

# --- Inline Keyboards ---


def get_broadcast_confirmation_keyboard() -> InlineKeyboardMarkup:  # NEW
    """Creates an inline keyboard for broadcast confirmation."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Yes, Broadcast Now", callback_data="broadcast_confirm:yes")
    builder.button(text="❌ No, Cancel Broadcast", callback_data="broadcast_confirm:no")
    builder.adjust(1)  # Make buttons full width for emphasis
    return builder.as_markup()


class EventKeyboardHandler:
    def __init__(self, event: Event, eligibility: EventUserEligibility, user: RevelUser) -> None:
        """This class handles the creation of keyboards for a specific event.

        Note: the event must prefetch the ticket_tiers
        """
        self.event = event
        self.user = user
        self.eligibility = eligibility
        self.site_settings = SiteSettings.get_solo()
        self.frontend_url = f"{self.site_settings.frontend_base_url}/events/{self.event.id}"
        self.builder = InlineKeyboardBuilder()
        self.builder.button(text="🎉 Event details", web_app=WebAppInfo(url=self.frontend_url))
        self.builder.adjust(1, 1)

    def get_eligible_keyboard(self) -> InlineKeyboardMarkup:
        """Get the eligible keyboard."""
        if self.eligibility.allowed:
            return self._get_keyboard_allowed()
        return self._get_keyboard_not_allowed()

    def _get_keyboard_allowed(self) -> InlineKeyboardMarkup:
        if self.event.requires_ticket:
            return self._get_keyboard_ticket()
        return self._get_keyboard_rsvp()

    def _get_keyboard_ticket(self) -> InlineKeyboardMarkup:
        # For ticketed events, only show the event details link (already added in __init__)
        # Users should purchase tickets through the frontend
        return self.builder.as_markup()

    def _get_keyboard_rsvp(self) -> InlineKeyboardMarkup:
        self.builder.button(text="✅ Going", callback_data=f"rsvp:{self.event.id}:yes")
        self.builder.button(text="❌ Not Going", callback_data=f"rsvp:{self.event.id}:no")
        self.builder.button(text="🤔 Maybe", callback_data=f"rsvp:{self.event.id}:maybe")
        self.builder.adjust(1, 2, 1)  # Two buttons on the first row, one on the second
        return self.builder.as_markup()

    def _get_keyboard_not_allowed(self) -> InlineKeyboardMarkup:
        # For users who are not eligible, show simple action buttons for common next steps
        # More complex flows (questionnaires, waitlist) should be done via the frontend
        if self.eligibility.next_step == NextStep.REQUEST_INVITATION:
            self.builder.button(
                text="💌 Request Invitation",
                callback_data=f"request_invitation:{self.event.id}",
            )
        elif self.eligibility.next_step == NextStep.BECOME_MEMBER:
            self.builder.button(
                text="👤 Become Member",
                callback_data=f"become_member:{self.event.organization_id}",
            )
        elif self.eligibility.next_step == NextStep.JOIN_WAITLIST:
            self.builder.button(
                text="⌛ Join Waitlist",
                callback_data=f"join_waitlist:{self.event.id}",
            )

        # For all other next steps, users should use the event details link (already added in __init__)
        return self.builder.as_markup()


def get_event_eligible_keyboard(
    event: Event, eligibility: EventUserEligibility, user: RevelUser
) -> InlineKeyboardMarkup:
    """Get the eligible keyboard."""
    handler = EventKeyboardHandler(event, eligibility, user)
    return handler.get_eligible_keyboard()

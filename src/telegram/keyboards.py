# src/telegram/keyboards.py


from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from django.conf import settings

from accounts.models import RevelUser
from events.models import Event
from events.service.event_manager import EventUserEligibility, NextStep

# --- Reply Keyboards ---


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Get main menu keyboard."""
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📚 Request New Story"))
    builder.row(KeyboardButton(text="⚙️ My Preferences"))
    # builder.row(KeyboardButton(text="📖 My Stories")) # Future feature
    return builder.as_markup(resize_keyboard=True)


def get_confirmation_keyboard() -> ReplyKeyboardMarkup:
    """Get confirmation keyboard."""
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="✅ Yes"), KeyboardButton(text="❌ No"))
    builder.row(KeyboardButton(text="🔙 Cancel"))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)


remove_keyboard = ReplyKeyboardRemove()  # Convenience object

# --- Inline Keyboards ---


def get_confirm_save_preference_keyboard() -> InlineKeyboardMarkup:
    """Creates an inline keyboard for confirming preference save/discard."""
    builder = InlineKeyboardBuilder()
    # Use distinct callback data for confirmation
    builder.button(text="✅ Yes, Save", callback_data="pref_save_confirm")
    builder.button(text="❌ No, Discard", callback_data="pref_save_cancel")
    builder.adjust(2)
    return builder.as_markup()


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
        self.builder = InlineKeyboardBuilder()
        self.builder.button(
            text="🎉 Event details", web_app=WebAppInfo(url=settings.BASE_URL + f"/{self.event.id}")
        )  # placeholder, will use reverse()
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
        for tier in self.event.ticket_tiers.all():
            self.builder.button(text=f"🎟️ Get Your Ticket: {tier.name}", callback_data=f"get_ticket:{tier.id}")
        return self.builder.as_markup()

    def _get_keyboard_rsvp(self) -> InlineKeyboardMarkup:
        self.builder.button(text="✅ Going", callback_data=f"rsvp:{self.event.id}:yes")
        self.builder.button(text="❌ Not Going", callback_data=f"rsvp:{self.event.id}:no")
        self.builder.button(text="🤔 Maybe", callback_data=f"rsvp:{self.event.id}:maybe")
        self.builder.adjust(1, 2, 1)  # Two buttons on the first row, one on the second
        return self.builder.as_markup()

    def _get_keyboard_not_allowed(self) -> InlineKeyboardMarkup:
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

        elif self.eligibility.next_step == NextStep.COMPLETE_QUESTIONNAIRE and self.eligibility.questionnaires_missing:
            questionnaire_id = self.eligibility.questionnaires_missing[0]
            self.builder.button(
                text="✍️ Start Questionnaire",
                web_app=WebAppInfo(url=f"https://placeholder.example.com/questionnaire/{questionnaire_id}"),
            )

        elif (
            self.eligibility.next_step == NextStep.WAIT_FOR_QUESTIONNAIRE_EVALUATION
            and self.eligibility.questionnaires_pending_review
        ):
            # questionnaire_id = self.eligibility.questionnaires_pending_review[0]
            self.builder.button(
                text="🕵️ Questionnaire is pending review",
                callback_data=f"wait_for_questionnaire_evaluation:{self.event.id}",
            )

        elif self.eligibility.next_step == NextStep.JOIN_WAITLIST:
            self.builder.button(
                text="⏳ Join Waitlist",
                callback_data=f"join_waitlist:{self.event.id}",
            )

        elif self.eligibility.next_step == NextStep.PURCHASE_TICKET:
            self.builder.button(
                text="💶 Purchase Ticket",
                callback_data=f"purchase_ticket:{self.event.id}",
            )

        else:
            self.builder.button(
                text="Oops. Unfortunately you are not (yet) eligible for this party.",
                callback_data=f"consolation_meme:{self.event.id}",
            )
        """Get the right keyboard with possible actions based on eligibility and next steps."""
        return self.builder.as_markup()


def get_event_eligible_keyboard(
    event: Event, eligibility: EventUserEligibility, user: RevelUser
) -> InlineKeyboardMarkup:
    """Get the eligible keyboard."""
    handler = EventKeyboardHandler(event, eligibility, user)
    return handler.get_eligible_keyboard()

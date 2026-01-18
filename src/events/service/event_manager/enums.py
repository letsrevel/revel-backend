"""Enums for the event eligibility system."""

from enum import StrEnum

from django.utils.translation import gettext_noop


class NextStep(StrEnum):
    """Possible next steps for a user to become eligible for an event."""

    REQUEST_INVITATION = "request_invitation"
    WAIT_FOR_INVITATION_APPROVAL = "wait_for_invitation_approval"
    BECOME_MEMBER = "become_member"
    COMPLETE_QUESTIONNAIRE = "complete_questionnaire"
    WAIT_FOR_QUESTIONNAIRE_EVALUATION = "wait_for_questionnaire_evaluation"
    WAIT_TO_RETAKE_QUESTIONNAIRE = "wait_to_retake_questionnaire"
    WAIT_FOR_EVENT_TO_OPEN = "wait_for_event_to_open"
    JOIN_WAITLIST = "join_waitlist"
    WAIT_FOR_OPEN_SPOT = "wait_for_open_spot"
    PURCHASE_TICKET = "purchase_ticket"
    RSVP = "rsvp"
    UPGRADE_MEMBERSHIP = "upgrade_membership"
    REQUEST_WHITELIST = "request_whitelist"
    WAIT_FOR_WHITELIST_APPROVAL = "wait_for_whitelist_approval"
    COMPLETE_PROFILE = "complete_profile"


class Reasons(StrEnum):
    """Reasons why a user is not eligible for an event.

    Note: Strings are marked with _noop() for translation extraction.
    The actual translation happens in gates.py when using _(Reasons.XXX).
    """

    MEMBERS_ONLY = gettext_noop("Only members are allowed.")
    MEMBERSHIP_INACTIVE = gettext_noop("Your membership is not active.")
    REQUIRES_FULL_PROFILE = gettext_noop("Requires full profile.")
    EVENT_IS_FULL = gettext_noop("Event is full.")
    SOLD_OUT = gettext_noop("Sold out")
    QUESTIONNAIRE_MISSING = gettext_noop("Questionnaire has not been filled.")
    QUESTIONNAIRE_FAILED = gettext_noop("Questionnaire evaluation was insufficient.")
    QUESTIONNAIRE_PENDING_REVIEW = gettext_noop("Waiting for questionnaire evaluation.")
    QUESTIONNAIRE_RETAKE_COOLDOWN = gettext_noop(
        "Questionnaire evaluation was insufficient. You can try again in {retry_on}."
    )
    REQUIRES_TICKET = gettext_noop("Requires a ticket.")
    MUST_RSVP = gettext_noop("Must RSVP")
    REQUIRES_INVITATION = gettext_noop("Requires invitation.")
    INVITATION_REQUEST_PENDING = gettext_noop("Your invitation request is pending approval.")
    INVITATION_REQUEST_REJECTED = gettext_noop("Your invitation request was rejected.")
    REQUIRES_PURCHASE = gettext_noop("Requires purchase.")
    NOTHING_TO_PURCHASE = gettext_noop("Nothing to purchase.")
    EVENT_IS_NOT_OPEN = gettext_noop("Event is not open.")
    EVENT_HAS_FINISHED = gettext_noop("Event has finished.")
    RSVP_DEADLINE_PASSED = gettext_noop("The RSVP deadline has passed.")
    APPLICATION_DEADLINE_PASSED = gettext_noop("The application deadline has passed.")
    NO_TICKETS_ON_SALE = gettext_noop("Tickets are not currently on sale.")
    MEMBERSHIP_TIER_REQUIRED = gettext_noop("This ticket tier requires a specific membership tier.")
    BLACKLISTED = gettext_noop("You are not allowed to participate in this organization's events.")
    VERIFICATION_REQUIRED = gettext_noop("Additional verification required.")
    WHITELIST_PENDING = gettext_noop("Your verification request is pending approval.")
    WHITELIST_REJECTED = gettext_noop("Your verification request was rejected.")

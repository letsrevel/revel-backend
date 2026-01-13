"""Enums for the event eligibility system."""

from enum import StrEnum


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


class Reasons(StrEnum):
    """Reasons why a user is not eligible for an event."""

    MEMBERS_ONLY = "Only members are allowed."
    MEMBERSHIP_INACTIVE = "Your membership is not active."
    EVENT_IS_FULL = "Event is full."
    SOLD_OUT = "Sold out"
    QUESTIONNAIRE_MISSING = "Questionnaire has not been filled."
    QUESTIONNAIRE_FAILED = "Questionnaire evaluation was insufficient."
    QUESTIONNAIRE_PENDING_REVIEW = "Waiting for questionnaire evaluation."
    QUESTIONNAIRE_RETAKE_COOLDOWN = "Questionnaire evaluation was insufficient. You can try again in {retry_on}."
    REQUIRES_TICKET = "Requires a ticket."
    MUST_RSVP = "Must RSVP"
    REQUIRES_INVITATION = "Requires invitation."
    INVITATION_REQUEST_PENDING = "Your invitation request is pending approval."
    INVITATION_REQUEST_REJECTED = "Your invitation request was rejected."
    REQUIRES_PURCHASE = "Requires purchase."
    NOTHING_TO_PURCHASE = "Nothing to purchase."
    EVENT_IS_NOT_OPEN = "Event is not open."
    EVENT_HAS_FINISHED = "Event has finished."
    RSVP_DEADLINE_PASSED = "The RSVP deadline has passed."
    APPLICATION_DEADLINE_PASSED = "The application deadline has passed."
    NO_TICKETS_ON_SALE = "Tickets are not currently on sale."
    MEMBERSHIP_TIER_REQUIRED = "This ticket tier requires a specific membership tier."
    BLACKLISTED = "You are not allowed to participate in this organization's events."
    VERIFICATION_REQUIRED = "Additional verification required."
    WHITELIST_PENDING = "Your verification request is pending approval."
    WHITELIST_REJECTED = "Your verification request was rejected."

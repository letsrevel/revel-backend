"""CI guard: account profile data is only projected for the account holder or a person who acted.

Scope: every operation whose response embeds ``MemberUserSchema`` / ``MinimalRevelUserSchema`` /
``AttendeeSchema`` (or a component that transitively embeds one), plus ``BlacklistEntrySchema`` (it resolves a
linked account's name). Profile data (phone, legal names, pronouns, bio, picture) may only leave through an operation
whose subject is the caller's own account, or a person who acted toward the organization (member, staff,
applicant, waitlist, invitation request, questionnaire submitter, series-pass holder, ...).
Staff-originated rows (box-office tickets, staff RSVPs, staff-made invitations) must use a masked
schema instead. Every operation exposing a carrier must be classified below.
"""

import typing as t

from api.api import api

SEED_CARRIERS = {"MemberUserSchema", "MinimalRevelUserSchema", "AttendeeSchema", "BlacklistEntrySchema"}

SELF = "the caller's own account"
MEMBER = "subject is a member/staff of the organization"
APPLICANT = "subject applied (membership request / invitation request / questionnaire)"
WAITLIST = "subject joined the waitlist"
WAITLIST_OFFER = "subject joined the waitlist; offers are only created from waitlist entries"
PASS_HOLDER = "subject holds (bought) a series pass"
BLACKLIST_ENTRY = "linked account's name shown only for a known holder; else what staff typed (BlacklistEntrySchema)"
ATTENDEE_OPT_IN = "attendees shown by their own opt-in; privileged view limited to known holders"

HOLDER_PROFILE_OPERATIONS: dict[tuple[str, str], str] = {
    ("get", "/api/organization-admin/{slug}/blacklist"): BLACKLIST_ENTRY,
    ("post", "/api/organization-admin/{slug}/blacklist"): BLACKLIST_ENTRY,
    ("get", "/api/organization-admin/{slug}/blacklist/{entry_id}"): BLACKLIST_ENTRY,
    ("patch", "/api/organization-admin/{slug}/blacklist/{entry_id}"): BLACKLIST_ENTRY,
    ("get", "/api/dashboard/invitation-requests"): SELF,
    ("get", "/api/events/{event_id}/attendee-list"): ATTENDEE_OPT_IN,
    ("post", "/api/events/{event_id}/invitation-requests"): SELF,
    ("post", "/api/organizations/{slug}/membership-requests"): SELF,
    ("get", "/api/event-admin/{event_id}/invitation-requests"): APPLICANT,
    ("get", "/api/organization-admin/{slug}/membership-requests"): APPLICANT,
    ("get", "/api/questionnaires/{org_questionnaire_id}/submissions"): APPLICANT,
    ("get", "/api/questionnaires/{org_questionnaire_id}/submissions/{submission_id}"): APPLICANT,
    ("get", "/api/organization-admin/{slug}/members"): MEMBER,
    ("get", "/api/organization-admin/{slug}/members/verify/{code}"): MEMBER,
    ("get", "/api/organization-admin/{slug}/members/{user_id}"): MEMBER,
    ("put", "/api/organization-admin/{slug}/members/{user_id}"): MEMBER,
    ("get", "/api/organization-admin/{slug}/staff"): MEMBER,
    ("post", "/api/organization-admin/{slug}/staff/{user_id}"): MEMBER,
    ("put", "/api/organization-admin/{slug}/staff/{user_id}/permissions"): MEMBER,
    ("get", "/api/event-admin/{event_id}/waitlist"): WAITLIST,
    ("get", "/api/event-admin/{event_id}/waitlist-offers"): WAITLIST_OFFER,
    ("post", "/api/event-admin/{event_id}/waitlist-offers"): WAITLIST_OFFER,
    ("post", "/api/event-admin/{event_id}/waitlist-offers/{offer_id}/reactivate"): WAITLIST_OFFER,
    ("post", "/api/event-admin/{event_id}/waitlist-offers/{offer_id}/revoke"): WAITLIST_OFFER,
    ("get", "/api/event-series-admin/{series_id}/passes/{pass_id}/holders"): PASS_HOLDER,
    ("post", "/api/event-series-admin/{series_id}/passes/held/{held_pass_id}/cancel"): PASS_HOLDER,
    ("post", "/api/event-series-admin/{series_id}/passes/held/{held_pass_id}/confirm-payment"): PASS_HOLDER,
    (
        "post",
        "/api/event-admin/{event_id}/tickets/{code}/check-in",
    ): MEMBER,  # member-card scan outcome (MemberVerificationSchema); the ticket outcome is masked
}


def _refs(node: t.Any) -> t.Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value.rsplit("/", 1)[-1]
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def _compute() -> tuple[set[str], set[tuple[str, str]]]:
    spec = api.get_openapi_schema()
    components = spec["components"]["schemas"]
    carriers = set(SEED_CARRIERS)
    changed = True
    while changed:
        changed = False
        for name, comp in components.items():
            if name not in carriers and set(_refs(comp)) & carriers:
                carriers.add(name)
                changed = True
    exposing = {
        (method, path)
        for path, path_item in spec["paths"].items()
        for method, operation in path_item.items()
        if isinstance(operation, dict) and set(_refs(operation)) & carriers
    }
    return carriers, exposing


def test_holder_profile_only_projected_for_holder_or_actor() -> None:
    _, exposing = _compute()
    expected = set(HOLDER_PROFILE_OPERATIONS)
    assert exposing == expected, (
        f"Operations newly exposing account profile data: {sorted(exposing - expected)}; "
        f"classified but no longer exposing: {sorted(expected - exposing)}. Classify new operations "
        "in HOLDER_PROFILE_OPERATIONS (subject must be the holder or someone who acted), or switch "
        "them to the masked schema (StaffViewUserSchema)."
    )


def test_guard_is_not_vacuous() -> None:
    carriers, exposing = _compute()
    assert SEED_CARRIERS <= carriers
    assert len(carriers) > len(SEED_CARRIERS)
    assert exposing

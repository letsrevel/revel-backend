"""CI guard: account profile data is only projected for the account holder or a person who acted.

Profile data (phone, legal names, pronouns, bio, picture) may only leave through an operation whose
subject is the caller's own account, or a person who acted toward the organization (member, staff,
applicant, waitlist, invitation request, questionnaire submitter, series-pass holder, ...).
Staff-originated rows (box-office tickets, staff RSVPs, staff-made invitations) must use a masked
schema instead. Carriers are ``MemberUserSchema`` / ``MinimalRevelUserSchema`` plus every component
that transitively embeds one; every operation exposing a carrier must be classified below.
"""

import typing as t

from api.api import api

SEED_CARRIERS = {"MemberUserSchema", "MinimalRevelUserSchema"}

SELF = "the caller's own account"
MEMBER = "subject is a member/staff of the organization"
APPLICANT = "subject applied (membership request / invitation request / questionnaire)"
WAITLIST = "subject joined the waitlist"
PASS_HOLDER = "subject holds (bought) a series pass"
PENDING_MASK = "TEMPORARY: switches to StaffViewUserSchema"

HOLDER_PROFILE_OPERATIONS: dict[tuple[str, str], str] = {
    ("get", "/api/dashboard/invitation-requests"): SELF,
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
    ("get", "/api/event-admin/{event_id}/waitlist-offers"): WAITLIST,
    ("post", "/api/event-admin/{event_id}/waitlist-offers"): WAITLIST,
    ("post", "/api/event-admin/{event_id}/waitlist-offers/{offer_id}/reactivate"): WAITLIST,
    ("post", "/api/event-admin/{event_id}/waitlist-offers/{offer_id}/revoke"): WAITLIST,
    ("get", "/api/event-series-admin/{series_id}/passes/{pass_id}/holders"): PASS_HOLDER,
    ("post", "/api/event-series-admin/{series_id}/passes/held/{held_pass_id}/cancel"): PASS_HOLDER,
    ("post", "/api/event-series-admin/{series_id}/passes/held/{held_pass_id}/confirm-payment"): PASS_HOLDER,
    # Staff-originated rows: must move to the masked schema, then these entries disappear.
    ("get", "/api/event-admin/{event_id}/tickets"): PENDING_MASK,
    ("get", "/api/event-admin/{event_id}/tickets/{ticket_id}"): PENDING_MASK,
    ("post", "/api/event-admin/{event_id}/seating/reseat"): PENDING_MASK,
    ("post", "/api/event-admin/{event_id}/tickets/{code}/check-in"): PENDING_MASK,
    ("get", "/api/event-admin/{event_id}/rsvps"): PENDING_MASK,
    ("get", "/api/event-admin/{event_id}/rsvps/{rsvp_id}"): PENDING_MASK,
    ("post", "/api/event-admin/{event_id}/rsvps"): PENDING_MASK,
    ("put", "/api/event-admin/{event_id}/rsvps/{rsvp_id}"): PENDING_MASK,
    ("get", "/api/event-admin/{event_id}/invitations"): PENDING_MASK,
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

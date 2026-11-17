# Eligibility Pipeline

The eligibility pipeline is the **most critical flow** in Revel. It determines whether a user can participate in an event by running a sequence of checks called **gates**. Each gate evaluates a specific condition and can either pass (return `None`), or fail by returning an `EventUserEligibility` result with `allowed=False` and an actionable `next_step`. Some gates can be bypassed by invitation waivers.

!!! danger "This is core business logic"
    Changes to the eligibility pipeline affect every user's ability to join events. Any modifications must be thoroughly tested and reviewed. The `EligibilityService` is the single source of truth for event access decisions.

## Pipeline Overview

The pipeline consists of **11 gates** executed in strict order. The first gate to return a result short-circuits the pipeline; no subsequent gates are evaluated.

```mermaid
flowchart TD
    Start([User requests to join event]) --> G1

    G1{"1. PrivilegedAccessGate<br/>Owner or Staff?"}
    G1 -->|"Yes"| Eligible([Eligible])
    G1 -->|"No"| G2

    G2{"2. BlacklistGate<br/>Blacklisted or fuzzy match?"}
    G2 -->|"Clear"| G3
    G2 -->|"Hard blacklisted"| Rejected1([Rejected: blacklisted])
    G2 -->|"Fuzzy match"| Rejected1b([Rejected: verification_required])

    G3{"3. EventStatusGate<br/>Event open & not ended?"}
    G3 -->|"Yes"| G4
    G3 -->|"No"| Rejected2([Rejected: event_not_open / finished])

    G4{"4. RSVPDeadlineGate<br/>RSVP deadline not passed?<br/>(non-ticket events only)"}
    G4 -->|"Yes / Waived / N/A"| G5
    G4 -->|"No"| Rejected3([Rejected: rsvp_deadline_passed])

    G5{"5. ApplyDeadlineGate<br/>Application deadline not passed?"}
    G5 -->|"Yes / Waived / Already applied"| G6
    G5 -->|"No & needs to apply"| Rejected4([Rejected: application_deadline_passed])

    G6{"6. InvitationGate<br/>Private event?"}
    G6 -->|"Public / Has invitation"| G7
    G6 -->|"Private & no invitation"| Rejected5([Rejected: requires_invitation])

    G7{"7. MembershipGate<br/>Members-only event?"}
    G7 -->|"Not members-only / Is member / Waived"| G8
    G7 -->|"Not a member"| Rejected6([Rejected: members_only /<br/>membership_application_pending /<br/>membership_questionnaire_pending_review])

    G8{"8. FullProfileGate<br/>Profile complete?"}
    G8 -->|"Yes / Not required"| G9
    G8 -->|"Incomplete"| Rejected7([Rejected: requires_full_profile])

    G9{"9. QuestionnaireGate<br/>Questionnaires passed?"}
    G9 -->|"All passed / Waived / None required"| G10
    G9 -->|"Incomplete"| Rejected8([Rejected: questionnaire status])

    G10{"10. AvailabilityGate<br/>Capacity available?"}
    G10 -->|"Yes / Overridden"| G11
    G10 -->|"No"| Rejected9([Rejected: event_is_full])

    G11{"11. TicketSalesGate<br/>Active sales window?"}
    G11 -->|"Yes / Non-ticketed"| Eligible
    G11 -->|"No"| Rejected10([Rejected: no_tickets_on_sale / sales_paused])

    style Eligible fill:#2e7d32,color:#fff
    style Rejected1 fill:#c62828,color:#fff
    style Rejected1b fill:#c62828,color:#fff
    style Rejected2 fill:#c62828,color:#fff
    style Rejected3 fill:#c62828,color:#fff
    style Rejected4 fill:#c62828,color:#fff
    style Rejected5 fill:#c62828,color:#fff
    style Rejected6 fill:#c62828,color:#fff
    style Rejected7 fill:#c62828,color:#fff
    style Rejected8 fill:#c62828,color:#fff
    style Rejected9 fill:#c62828,color:#fff
    style Rejected10 fill:#c62828,color:#fff
```

## Gate Details

### 1. PrivilegedAccessGate

The first gate provides a **fast path** for organization owners and staff. If the requesting user is the organization owner or a staff member, they are immediately granted access with `allowed=True`; no further gates are checked.

!!! tip "Why this is first"
    Owners and staff always need access to their own events for management purposes (check-in, monitoring, testing). Placing this gate first avoids unnecessary computation.

**Source:** `events/service/event_manager/gates.py`: `PrivilegedAccessGate`

---

### 2. BlacklistGate

Checks whether the user is blocked from participating in the organization's events. This gate has two levels of blocking:

1. **Hard block**: The user is definitively blacklisted (FK match or hard identifier match). Returns `allowed=False` with no `next_step` and no recourse.
2. **Soft block (fuzzy match)**: The user's name fuzzy-matches a blacklist entry. This requires verification through a whitelist request flow:
    - **Active members** bypass fuzzy matching entirely (trusted users).
    - If fuzzy-matched but **already whitelisted**, the gate passes.
    - If a **whitelist request is pending**, returns `next_step=WAIT_FOR_WHITELIST_APPROVAL`.
    - If a **whitelist request was rejected**, returns no `next_step` (no recourse).
    - If **no request exists yet**, returns `next_step=REQUEST_WHITELIST`.

!!! note
    This gate cannot be waived by invitations. Blacklist checks are absolute.

**Source:** `events/service/event_manager/gates.py`: `BlacklistGate`

---

### 3. EventStatusGate

Checks the fundamental state of the event:

- Has the event's **end date** passed? If so, returns reason `EVENT_HAS_FINISHED` with no `next_step`.
- Is the event **status** set to `OPEN`? If not, returns reason `EVENT_IS_NOT_OPEN` with `next_step=WAIT_FOR_EVENT_TO_OPEN`.

!!! note
    This gate cannot be waived. A closed or ended event is closed for everyone.

**Source:** `events/service/event_manager/gates.py`: `EventStatusGate`

---

### 4. RSVPDeadlineGate

For **non-ticketed events**, checks whether the RSVP deadline (`event.rsvp_before`) has passed.

- **Ticketed events**: gate is skipped entirely (returns `None`).
- **No deadline set**: gate passes.
- **Deadline passed**: returns `allowed=False` with reason `RSVP_DEADLINE_PASSED`.

!!! info "Invitation waiver: `waives_rsvp_deadline`"
    This gate **can be waived** by an `EventInvitation` with `waives_rsvp_deadline=True`. Invited users can RSVP even after the deadline has passed.

**Source:** `events/service/event_manager/gates.py`: `RSVPDeadlineGate`

---

### 5. ApplyDeadlineGate

Checks whether the **application deadline** has passed for users who still need to apply. The effective deadline is `event.apply_before` if set, otherwise falls back to the event start time (`event.effective_apply_deadline`).

This gate only blocks users who have **not yet completed** their required application steps:

- Needs to submit an **invitation request** (private event that accepts requests, user has no invitation and no existing request).
- Needs to complete a required **questionnaire** (admission questionnaire not yet submitted or previously rejected).

Users who have already applied (submitted a request or completed questionnaires) pass through even after the deadline.

!!! info "Invitation waiver: `waives_apply_deadline`"
    This gate **can be waived** by an `EventInvitation` with `waives_apply_deadline=True`.

**Source:** `events/service/event_manager/gates.py`: `ApplyDeadlineGate`

---

### 6. InvitationGate

For **private events**, ensures the user has a valid `EventInvitation`.

- **Public / members-only events**: gate is skipped entirely.
- **Has invitation**: gate passes.
- **Pending invitation request**: returns `next_step=WAIT_FOR_INVITATION_APPROVAL`.
- **Rejected invitation request**: returns no `next_step` (no recourse).
- **No invitation and no request**: returns `next_step=REQUEST_INVITATION` if the event accepts invitation requests, otherwise no `next_step`.

**Source:** `events/service/event_manager/gates.py`: `InvitationGate`

---

### 7. MembershipGate

For **members-only events**, ensures the user is an active member of the organization.

- **Not a members-only event**: gate is skipped.
- **User is an active member**: gate passes.
- **User has inactive membership** (anything but ACTIVE, e.g. PAUSED or AWAITING_PAYMENT): returns reason `MEMBERSHIP_INACTIVE` with no `next_step`.
- **User has no membership** but a membership attempt in flight (#1300): the gate asks `EligibilityService.pending_membership_step`, which delegates to the org's [`MembershipEligibilityService`](membership-eligibility.md) so the event never contradicts the membership page. Only two waiting steps surface:
    - application awaiting staff → `MEMBERSHIP_APPLICATION_PENDING` with `next_step=WAIT_FOR_MEMBERSHIP_APPROVAL`;
    - membership questionnaire awaiting evaluation → `MEMBERSHIP_QUESTIONNAIRE_PENDING_REVIEW` with `next_step=WAIT_FOR_MEMBERSHIP_QUESTIONNAIRE_EVALUATION`.
- **User has no membership and nothing in flight**: returns reason `MEMBERS_ONLY` with `next_step=BECOME_MEMBER` if the organization accepts membership requests. Anything else the user must act on (submit or retake a questionnaire, reapply) also lands here.

!!! info "Invitation waiver: `waives_membership_required`"
    This gate **can be waived** by an `EventInvitation` with `waives_membership_required=True`. Non-members can participate in members-only events if they were explicitly invited with this waiver.

**Source:** `events/service/event_manager/gates.py`: `MembershipGate`

---

### 8. FullProfileGate

When an event has `requires_full_profile=True`, ensures the user has a complete profile:

- **Profile picture** present
- **Pronouns** set
- **Name** provided (at least one of `first_name`, `last_name`, or `preferred_name`)

If any fields are missing, returns `allowed=False` with `next_step=COMPLETE_PROFILE` and a `missing_profile_fields` list indicating which fields need to be filled in (e.g., `["profile_picture", "pronouns", "name"]`).

!!! note
    This gate cannot be waived by invitations.

**Source:** `events/service/event_manager/gates.py`: `FullProfileGate`

---

### 9. QuestionnaireGate

Checks whether the user has submitted and passed all required admission questionnaires linked to the event's organization. This gate performs three sub-checks in order:

1. **Missing questionnaires**: User has not submitted a required questionnaire, or their approved submission has expired (`max_submission_age`). Returns `next_step=COMPLETE_QUESTIONNAIRE` with `questionnaires_missing` list.
2. **Pending review**: Submission exists but evaluation is `PENDING_REVIEW` or not yet created. Returns `next_step=WAIT_FOR_QUESTIONNAIRE_EVALUATION` with `questionnaires_pending_review` list. **Skipped for questionnaires with `requires_evaluation=False`.**
3. **Failed questionnaires**: Evaluation was `REJECTED`. **Skipped for questionnaires with `requires_evaluation=False`.**
    - If max attempts exceeded, returns `questionnaires_failed` list with no `next_step`.
    - If retake is allowed and cooldown has passed (or `can_retake_after` is `null`/zero for immediate retake), adds to `questionnaires_missing` for resubmission.
    - If retake is allowed but cooldown has not passed, returns `next_step=WAIT_TO_RETAKE_QUESTIONNAIRE` with `retry_on` datetime.

**Questionnaire scoping:**

- **Global questionnaires**: Submission is shared across all events.
- **Per-event questionnaires** (`per_event=True`): Submission is scoped to the specific event.
- **Member exemptions** (`members_exempt=True`): Active members skip the questionnaire.
- **No evaluation required** (`requires_evaluation=False`): Only submission existence is checked — pending review and failed checks are skipped entirely. See [Questionnaires: Information-Gathering Mode](questionnaires.md#information-gathering-mode-requires_evaluation).

!!! info "Invitation waiver: `waives_questionnaire`"
    This gate **can be waived** by an `EventInvitation` with `waives_questionnaire=True`. All questionnaire requirements are bypassed.

!!! warning "Questionnaires can use AI evaluation"
    Some questionnaires use LLM-powered evaluation. See [Questionnaires](questionnaires.md) for details on evaluation modes and scoring.

**Source:** `events/service/event_manager/gates.py`: `QuestionnaireGate`

---

### 10. AvailabilityGate

Checks whether the event has reached its capacity limit. Uses `event.effective_capacity`, which is the minimum of `max_attendees` and `venue.capacity` (or whichever is set; 0 means unlimited).

- **Unlimited capacity** (`effective_capacity == 0`): gate passes.
- **Counting logic:**
    - **Ticketed events**: counts non-cancelled tickets.
    - **RSVP events**: counts `YES` RSVPs.
    - **Pending unexpired non-cutoff waitlist offers** also count toward capacity (minus the current user's own offer, if any) — see [Advanced Waitlist](waitlist.md).
- **Event full**: returns `next_step=JOIN_WAITLIST` if waitlist is open and user is not already waitlisted, `next_step=WAIT_FOR_OPEN_SPOT` if already on the waitlist, or no `next_step` if no waitlist.
- **Reservation-driven fullness**: when seats exist on paper but are held by waitlist offers, the gate returns `SPOTS_RESERVED_FOR_WAITLIST` (or `ON_WAITLIST_WAITING_FOR_BATCH` for users already on the queue) instead of plain `EVENT_IS_FULL`.

!!! info "Invitation waiver: `overrides_max_attendees`"
    This gate **can be bypassed** by an `EventInvitation` with `overrides_max_attendees=True`. Invited users can join even when the event is technically full.

!!! note "Two-phase capacity check"
    This gate performs a **preliminary** check using prefetched in-memory data (zero DB queries). The final authoritative capacity check happens in `EventManager._assert_capacity()` within a database transaction with row-level locking (`select_for_update`) to prevent race conditions. Both layers apply the same offer-aware arithmetic; see [Advanced Waitlist → Capacity & reservation semantics](waitlist.md#capacity-reservation-semantics).

**Source:** `events/service/event_manager/gates.py`: `AvailabilityGate`

---

### 11. TicketSalesGate

For **ticketed events**, checks whether there is at least one ticket tier with an active sales window.

- **Non-ticketed events**: gate is skipped.
- For each tier, checks if the current time falls within the sales window:
    - `sales_start_at` to `sales_end_at` (falls back to `event.start` if `sales_end_at` is not set).
    - Tiers with no sales window configured are assumed to always be on sale.
- Tiers with `sales_paused=True` are skipped (the pause is a per-tier kill switch independent of the window).
- If **no tier** has active sales, returns reason `NO_TICKETS_ON_SALE` with no `next_step`. When the event has tiers and **all** of them are paused, the reason is `SALES_PAUSED` instead.

!!! note
    This gate cannot be waived by invitations. Ticket sales windows are strict.

**Source:** `events/service/event_manager/gates.py`: `TicketSalesGate`

---

## Response Structure

When a user fails a gate, the `EligibilityService` returns an `EventUserEligibility` object (a Pydantic `BaseModel`):

```python
class EventUserEligibility(BaseModel):
    allowed: bool
    event_id: uuid.UUID
    reason: str | None = None
    reason_code: ReasonCode | None = None
    next_step: NextStep | None = None
    questionnaires_missing: list[uuid.UUID] | None = None
    questionnaires_pending_review: list[uuid.UUID] | None = None
    questionnaires_failed: list[uuid.UUID] | None = None
    retry_on: AwareDatetime | None = None
    missing_profile_fields: list[str] | None = None
    pending_offers_count: int | None = None
    next_batch_at: AwareDatetime | None = None
    waitlist_position: int | None = None
    active_offer_expires_at: AwareDatetime | None = None
    event_remaining: int | None = None
    questionnaire_events: list[SeriesPassQuestionnaireEventSchema] | None = None
```

| Field | Type | Description |
|---|---|---|
| `allowed` | `bool` | Whether the user can participate |
| `event_id` | `UUID` | The event being checked |
| `reason` | `str \| None` | Translated human-readable reason (from `Reasons` enum) — for display only |
| `reason_code` | `ReasonCode \| None` | Stable machine-readable identifier mirroring `reason` (snake_case, never translated) — switch on this in clients |
| `next_step` | `NextStep \| None` | Suggested action the user can take to become eligible |
| `questionnaires_missing` | `list[UUID] \| None` | IDs of questionnaires the user needs to complete |
| `questionnaires_pending_review` | `list[UUID] \| None` | IDs of questionnaires awaiting evaluation |
| `questionnaires_failed` | `list[UUID] \| None` | IDs of questionnaires the user has permanently failed |
| `retry_on` | `AwareDatetime \| None` | When the user can retake a failed questionnaire |
| `missing_profile_fields` | `list[str] \| None` | Profile fields that need to be completed (e.g., `profile_picture`, `pronouns`, `name`) |
| `pending_offers_count` | `int \| None` | `AvailabilityGate` only: pending unexpired waitlist offers currently reserving seats |
| `next_batch_at` | `AwareDatetime \| None` | `AvailabilityGate` only: earliest expiry among those offers, i.e. when seats may free up (in the user's or event's timezone) |
| `waitlist_position` | `int \| None` | `AvailabilityGate` only: the caller's place in the queue when already waitlisted |
| `active_offer_expires_at` | `AwareDatetime \| None` | Set on an **allowed** result when the caller holds an active waitlist offer: when it expires |
| `event_remaining` | `int \| None` | Event-scoped per-user ticket budget (#901); populated only by the `GET /my-status` path |
| `questionnaire_events` | `list[SeriesPassQuestionnaireEventSchema] \| None` | Series-pass checkout only (#1175): one `{questionnaire_id, event_id}` per missing questionnaire, naming the event whose questionnaire endpoint accepts it (see [Series Passes](series-passes.md)) |

!!! note
    On success (`allowed=True`), all optional fields default to `None`, except
    `active_offer_expires_at` when the caller holds a waitlist offer.

---

## NextStep Enum Values

All possible `NextStep` values and when they are returned:

| NextStep | Gate | When Suggested |
|---|---|---|
| `REQUEST_WHITELIST` | BlacklistGate | User fuzzy-matches a blacklist entry and needs to request verification |
| `WAIT_FOR_WHITELIST_APPROVAL` | BlacklistGate | Whitelist request is pending |
| `WAIT_FOR_EVENT_TO_OPEN` | EventStatusGate | Event exists but is not yet open |
| `WAIT_FOR_INVITATION_APPROVAL` | InvitationGate | Invitation request has been submitted and is pending |
| `REQUEST_INVITATION` | InvitationGate | Private event, user can request an invitation |
| `BECOME_MEMBER` | MembershipGate | Members-only event, organization accepts membership requests |
| `WAIT_FOR_MEMBERSHIP_APPROVAL` | MembershipGate | The user's membership application is awaiting staff |
| `WAIT_FOR_MEMBERSHIP_QUESTIONNAIRE_EVALUATION` | MembershipGate | The user's membership questionnaire is awaiting evaluation |
| `UPGRADE_MEMBERSHIP` | Ticket purchase (`batch_ticket_service/eligibility.py`) | The tier is restricted to specific membership tiers and the buyer holds none of them (`membership_tier_required`) |
| `CHOOSE_ANOTHER_TIER` | Ticket purchase (`batch_ticket_service/eligibility.py`) | The tier is non-members-only and the buyer's membership is excluded (`members_excluded`) |
| `COMPLETE_PROFILE` | FullProfileGate | User profile is missing required fields |
| `COMPLETE_QUESTIONNAIRE` | QuestionnaireGate | User has not completed required questionnaires |
| `WAIT_FOR_QUESTIONNAIRE_EVALUATION` | QuestionnaireGate | Questionnaire submitted, awaiting evaluation |
| `WAIT_TO_RETAKE_QUESTIONNAIRE` | QuestionnaireGate | Failed questionnaire, retake cooldown has not elapsed |
| `JOIN_WAITLIST` | AvailabilityGate | Event is full, waitlist is open, user is not on it |
| `WAIT_FOR_OPEN_SPOT` | AvailabilityGate | Event is full, user is already on the waitlist |
| `PURCHASE_TICKET` | EventManager | Returned by `EventManager.rsvp()` when event requires a ticket |
| `RSVP` | EventManager | Returned when event requires RSVP |

---

## Tier-Level Purchase Rules

The gates decide whether a user may reach the event; which **ticket tiers** they may buy is a separate layer that checkout re-asserts. `service/tier_eligibility.py` (`get_eligible_tiers`, used by the tier listing and `GET /my-status`) and `service/batch_ticket_service/eligibility.py` (the purchase-time asserts) apply the same rules. Owners and staff are exempt from all of them.

| Rule | Field | Effect |
|---|---|---|
| Visibility | `visibility` | `MEMBERS_ONLY` tiers are visible to ACTIVE **and** PAUSED members (`is_visible_status`); `PRIVATE` tiers to invitees (optionally only those whose invitation links the tier) |
| Sales window / pause | `sales_start_at`, `sales_end_at`, `sales_paused` | A paused tier is unbuyable for everyone; 403 "Ticket sales are paused." at checkout |
| `purchasable_by` | `PUBLIC`, `MEMBERS`, `INVITED`, `INVITED_AND_MEMBERS`, `NON_MEMBERS` | `MEMBERS` needs an ACTIVE membership; `INVITED` an invitation (linked to the tier when `restrict_purchase_to_linked_invitations`); `NON_MEMBERS` shuts members out. Failure is a 403 |
| `restricted_to_membership_tiers` | M2M | The buyer's ACTIVE membership tier must be one of them; otherwise `membership_tier_required` + `UPGRADE_MEMBERSHIP` (400 with the eligibility payload) |
| `excluded_membership_tiers` | M2M, `NON_MEMBERS` tiers only | Narrows who a non-members-only tier excludes: with none named, any eligible member is excluded; with some named, only members of those tiers. An excluded buyer gets `members_excluded` + `CHOOSE_ANOTHER_TIER` (400 with the eligibility payload) |

`utils/purchase_rules.py` (`tier_rule_stricter_than_pass`) compares two `purchasable_by` rules by who they admit; [series passes](series-passes.md) use it to refuse linking a tier stricter than the pass. `excluded_membership_tiers` is deliberately not a stricter signal.

### Membership access predicates

"Is this user a member" is never an inline `status` filter. `events/utils/membership_access.py` defines the named predicates and queryset builders every read goes through:

| Predicate | Statuses | Used for |
|---|---|---|
| *visible* (`is_visible_status`, `visible_memberships`) | ACTIVE, PAUSED | Seeing member-only content (event and tier visibility) |
| *eligible* (`is_eligible_status`, `eligible_memberships`) | ACTIVE | Acting as a member: buying member tiers, questionnaire exemptions, member comms |
| *awaiting payment* / *payment held* | AWAITING_PAYMENT / AWAITING_PAYMENT + ACTIVE | The offline join flow (see [Membership Subscriptions](membership-subscriptions.md#offline-join-flow)) |
| *banned* (`is_banned_status`, `banned_memberships`) | BANNED | Shutting people out |

`tests/test_membership_access_guard.py` walks the AST and fails when new code reintroduces an inline status filter, a `MembershipStatus.<X>` argument to `filter`/`exclude`/`Q`, or a manager call such as `.active_only()` outside its allowlists.

---

## Invitation Waivers Summary

`EventInvitation` objects carry boolean flags that can bypass specific gates. These are set per-invitation when an organizer creates the invitation.

!!! info "Which gates can invitations bypass?"

    | Gate | Waiver Field | Effect |
    |---|---|---|
    | PrivilegedAccessGate | N/A | Already a fast path for owners/staff |
    | BlacklistGate | None | Cannot be waived |
    | EventStatusGate | None | Cannot be waived |
    | RSVPDeadlineGate | `waives_rsvp_deadline` | Invited user can RSVP after the deadline |
    | ApplyDeadlineGate | `waives_apply_deadline` | Invited user can apply after the deadline |
    | InvitationGate | N/A | This is the invitation check itself |
    | MembershipGate | `waives_membership_required` | Non-member can join members-only event |
    | FullProfileGate | None | Cannot be waived |
    | QuestionnaireGate | `waives_questionnaire` | All questionnaire requirements bypassed |
    | AvailabilityGate | `overrides_max_attendees` | Invited user can join a full event |
    | TicketSalesGate | None | Cannot be waived |

Additionally, `waives_purchase` is available on invitations and checked by `EligibilityService.waives_purchase()`. It grants complimentary access and is used downstream in the ticket checkout flow, not within the eligibility gates themselves.

---

## Architecture

### EligibilityService

The `EligibilityService` class (`events/service/event_manager/service.py`) is the orchestrator. It:

1. **Pre-fetches all data** in `__init__` using optimized queries with `select_related`, `prefetch_related`, and annotations, ensuring subsequent gate checks require **zero additional database queries**.
2. **Instantiates all 11 gates** in order, passing itself as a handler so gates can access shared prefetched data.
3. **Runs gates sequentially** in `check_eligibility()`. The first gate to return an `EventUserEligibility` short-circuits the pipeline.
4. If all gates pass, returns `EventUserEligibility(allowed=True, event_id=...)`.

```python
def check_eligibility(self, bypass: bool = False) -> EventUserEligibility:
    if bypass:
        return EventUserEligibility(allowed=True, event_id=self.event.pk)
    for gate in self._gates:
        if result := gate.check():
            return result
    return EventUserEligibility(allowed=True, event_id=self.event.id)
```

### EventManager

The `EventManager` class (`events/service/event_manager/manager.py`) wraps `EligibilityService` and adds transactional operations:

- **`rsvp()`**: Checks eligibility, then asserts capacity with row-level locking, then creates/updates the RSVP. Users who already have a `YES` RSVP bypass eligibility checks when changing status (to prevent them from being "trapped" if requirements change).
- **`check_eligibility()`**: Delegates to `EligibilityService.check_eligibility()` with an optional `raise_on_false` parameter that raises `UserIsIneligibleError`.

### Sequence Diagram

```mermaid
sequenceDiagram
    actor User
    participant API as Controller
    participant EM as EventManager
    participant ES as EligibilityService
    participant Gates as Gates (1-11)
    participant DB as Database

    User->>API: POST /events/{id}/rsvp
    API->>EM: rsvp(user, event)
    EM->>ES: __init__(user, event)
    ES->>DB: Prefetch all data (optimized queries)
    ES-->>EM: Ready

    EM->>ES: check_eligibility()
    loop For each gate (1-11)
        ES->>Gates: gate.check()
        alt Gate blocks
            Gates-->>ES: EventUserEligibility(allowed=False)
            ES-->>EM: Return result (short-circuit)
        else Gate passes
            Gates-->>ES: None (continue)
        end
    end
    ES-->>EM: EventUserEligibility(allowed=True)

    alt Eligible
        EM->>DB: _assert_capacity() with SELECT FOR UPDATE
        EM->>DB: Create/update RSVP
        EM-->>API: EventRSVP
        API-->>User: 200 OK
    else Not eligible
        EM-->>API: UserIsIneligibleError
        API-->>User: 403 with reason & next_step
    end
```

---

## Reason Codes

Reasons are emitted on the wire in two forms:

- `reason` — the English message translated at runtime via `gettext`. **Display only** — locale and prose can change.
- `reason_code` — a stable snake_case identifier from the `ReasonCode` enum. **Switch on this** in clients; it never changes when prose or translation does.

| `ReasonCode` value | English message |
|---|---|
| `blacklisted` | "You are not allowed to participate in this organization's events." |
| `verification_required` | "Additional verification required." |
| `whitelist_pending` | "Your verification request is pending approval." |
| `whitelist_rejected` | "Your verification request was rejected." |
| `event_has_finished` | "Event has finished." |
| `event_is_not_open` | "Event is not open." |
| `rsvp_deadline_passed` | "The RSVP deadline has passed." |
| `application_deadline_passed` | "The application deadline has passed." |
| `requires_invitation` | "Requires invitation." |
| `invitation_request_pending` | "Your invitation request is pending approval." |
| `invitation_request_rejected` | "Your invitation request was rejected." |
| `members_only` | "Only members are allowed." |
| `membership_inactive` | "Your membership is not active." |
| `membership_tier_required` | "This ticket tier requires a specific membership tier." |
| `requires_full_profile` | "Requires full profile." |
| `questionnaire_missing` | "Questionnaire has not been filled." |
| `questionnaire_pending_review` | "Waiting for questionnaire evaluation." |
| `questionnaire_failed` | "Questionnaire evaluation was insufficient." |
| `questionnaire_retake_cooldown` | "Questionnaire evaluation was insufficient. You can try again in {retry_on}." |
| `membership_application_pending` | "Your membership application is awaiting approval." |
| `membership_questionnaire_pending_review` | "Your membership questionnaire is being reviewed." |
| `event_is_full` | "Event is full." |
| `spots_reserved_for_waitlist` | "Spots are currently reserved for waitlist members." |
| `on_waitlist_waiting_for_batch` | "You are on the waitlist. Waiting for your turn." |
| `sold_out` | "Sold out" |
| `no_tickets_on_sale` | "Tickets are not currently on sale." |
| `sales_paused` | "Ticket sales are paused." |
| `members_excluded` | "This ticket tier is not available to members." |
| `requires_ticket` | "Requires a ticket." |
| `must_rsvp` | "Must RSVP" |
| `requires_purchase` | "Requires purchase." |
| `nothing_to_purchase` | "Nothing to purchase." |

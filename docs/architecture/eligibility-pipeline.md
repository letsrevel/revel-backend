# Eligibility Pipeline

The eligibility pipeline is the **most critical flow** in Revel. It determines whether a user can participate in an event by running a sequence of checks called **gates**. Each gate evaluates a specific condition and can either pass, fail with a reason, or be waived by special circumstances (such as an invitation).

!!! danger "This is core business logic"
    Changes to the eligibility pipeline affect every user's ability to join events. Any modifications must be thoroughly tested and reviewed. The `EligibilityService` is the single source of truth for event access decisions.

## Pipeline Overview

```mermaid
flowchart TD
    Start([User requests to join event]) --> G1

    G1{"1. Privileged Access Gate<br/>Owner or Staff?"}
    G1 -->|"Yes"| Eligible([Eligible])
    G1 -->|"No"| G2

    G2{"2. Event Status Gate<br/>Event open & not ended?"}
    G2 -->|"Yes"| G3
    G2 -->|"No"| Rejected1([Rejected: event_not_open])

    G3{"3. RSVP Deadline Gate<br/>Deadline not passed?"}
    G3 -->|"Yes / Waived"| G4
    G3 -->|"No"| Rejected2([Rejected: rsvp_deadline_passed])

    G4{"4. Invitation Gate<br/>Private event?"}
    G4 -->|"Public / Has invitation"| G5
    G4 -->|"Private & no invitation"| Rejected3([Rejected: invitation_required])

    G5{"5. Membership Gate<br/>Members-only event?"}
    G5 -->|"Not members-only / Is member / Waived"| G6
    G5 -->|"Not a member"| Rejected4([Rejected: membership_required])

    G6{"6. Questionnaire Gate<br/>Required questionnaires passed?"}
    G6 -->|"All passed / None required"| G7
    G6 -->|"Incomplete"| Rejected5([Rejected: questionnaire_incomplete])

    G7{"7. Availability Gate<br/>Capacity available?"}
    G7 -->|"Yes / Waived"| G8
    G7 -->|"No"| Rejected6([Rejected: event_full])

    G8{"8. Ticket Sales Gate<br/>Active sales window?"}
    G8 -->|"Yes / Non-ticketed"| Eligible
    G8 -->|"No"| Rejected7([Rejected: tickets_not_on_sale])

    style Eligible fill:#2e7d32,color:#fff
    style Rejected1 fill:#c62828,color:#fff
    style Rejected2 fill:#c62828,color:#fff
    style Rejected3 fill:#c62828,color:#fff
    style Rejected4 fill:#c62828,color:#fff
    style Rejected5 fill:#c62828,color:#fff
    style Rejected6 fill:#c62828,color:#fff
    style Rejected7 fill:#c62828,color:#fff
```

## Gate Details

### 1. Privileged Access Gate

The first gate provides a **fast path** for organization owners and staff. If the requesting user is an owner or staff member of the organization that owns the event, they are immediately granted access -- no further gates are checked.

!!! tip "Why this is first"
    Owners and staff always need access to their own events for management purposes (check-in, monitoring, testing). Placing this gate first avoids unnecessary computation.

---

### 2. Event Status Gate

Checks the fundamental state of the event:

- Is the event **published** and **open** for registration?
- Has the event's **end date** not passed?
- Is the event in a valid state (not cancelled, not draft)?

!!! note
    This gate cannot be waived. A closed or ended event is closed for everyone.

---

### 3. RSVP Deadline Gate

For non-ticketed events, checks whether the RSVP deadline has passed.

!!! info "Invitation waiver"
    This gate **can be waived** by a valid `EventInvitation`. Invited users can RSVP even after the deadline has passed.

---

### 4. Invitation Gate

For **private events**, checks whether the user has a valid `EventInvitation`.

- Public events: gate is skipped entirely
- Private events without invitation: rejected with `invitation_required`
- Private events with invitation: proceed to next gate

!!! note
    Invitations are one-time use and tied to a specific user (by email or account).

---

### 5. Membership Gate

For **members-only events**, checks whether the user is an active member of the organization.

!!! info "Invitation waiver"
    This gate **can be waived** by a valid `EventInvitation`. Non-members can participate in members-only events if they were explicitly invited.

---

### 6. Questionnaire Gate

Checks whether the user has **submitted and passed** all required questionnaires for the event.

- If no questionnaires are required, the gate passes
- If questionnaires exist but are incomplete, returns `next_step: COMPLETE_QUESTIONNAIRE`
- If questionnaires were submitted but failed, returns the failure reason

!!! warning "Questionnaires can use AI evaluation"
    Some questionnaires use LLM-powered evaluation. See [Questionnaires](questionnaires.md) for details on evaluation modes and scoring.

---

### 7. Availability Gate

Checks whether the event has reached its `max_attendees` limit.

!!! info "Invitation waiver"
    This gate **can be waived** by a valid `EventInvitation`. Invited users can join even when the event is technically full.

When the event is full and the user is not invited, the service may suggest `next_step: JOIN_WAITLIST` if a waitlist is enabled.

---

### 8. Ticket Sales Gate

For **ticketed events**, checks whether there is at least one ticket tier with an active sales window (i.e., the current time falls between `sales_start` and `sales_end`).

- Non-ticketed events: gate is skipped
- No active sales window: rejected with `tickets_not_on_sale`

!!! note
    This gate cannot be waived by invitations. Ticket sales windows are strict.

## Response Structure

When a user fails a gate, the `EligibilityService` returns an `EventUserEligibility` object containing:

| Field | Description |
|---|---|
| `eligible` | `False` -- the user cannot participate |
| `reason` | Machine-readable reason code (e.g., `event_full`, `invitation_required`) |
| `message` | Human-readable explanation (localized) |
| `next_step` | Optional suggested action the user can take |

### Possible Next Steps

| Next Step | When Suggested |
|---|---|
| `COMPLETE_QUESTIONNAIRE` | User has not completed required questionnaires |
| `JOIN_WAITLIST` | Event is full but has a waitlist enabled |
| `REQUEST_INVITATION` | Private event, user can request an invitation |
| `JOIN_ORGANIZATION` | Members-only event, user can apply for membership |
| `PURCHASE_TICKET` | Ticketed event with active sales window |

## Invitation Waivers Summary

!!! info "Which gates can invitations bypass?"

    | Gate | Waivable? |
    |---|---|
    | Privileged Access | N/A (already a fast path) |
    | Event Status | No |
    | RSVP Deadline | **Yes** |
    | Invitation | N/A (this is the invitation check itself) |
    | Membership | **Yes** |
    | Questionnaire | No |
    | Availability | **Yes** |
    | Ticket Sales | No |

## Sequence Diagram

```mermaid
sequenceDiagram
    actor User
    participant API as Controller
    participant ES as EligibilityService
    participant DB as Database

    User->>API: POST /events/{id}/rsvp
    API->>ES: check_eligibility(user, event)

    ES->>DB: Get user's membership & role
    alt Owner or Staff
        ES-->>API: Eligible (fast path)
    else Regular user
        ES->>DB: Check event status
        ES->>DB: Check RSVP deadline + invitation
        ES->>DB: Check membership + invitation
        ES->>DB: Check questionnaire submissions
        ES->>DB: Check attendee count + invitation
        ES->>DB: Check ticket sales windows
        ES-->>API: EventUserEligibility
    end

    alt Eligible
        API->>API: Process RSVP / ticket
        API-->>User: 200 OK
    else Not eligible
        API-->>User: 403 with reason & next_step
    end
```

# Bootstrap Test Events - Eligibility Testing

This bootstrap command (`bootstrap_test_events`) creates a comprehensive set of test events designed to validate every eligibility gate and access control scenario in the Revel platform.

## Quick Start

```bash
# Run the command
python manage.py bootstrap_test_events
```

## What Gets Created

### 1. Test Users

| User | Email | Role | Password |
|------|-------|------|----------|
| Random Tester | `random.tester@example.com` | No organization affiliation | `password123` |
| Test Admin | `test.admin@example.com` | Owner of test org | `password123` |
| Test Staff | `test.staff@example.com` | Staff of test org | `password123` |
| Test Member | `test.member@example.com` | Member of test org | `password123` |

### 2. Test Organization

- **Name:** Eligibility Test Organization
- **Slug:** `eligibility-test-org`
- **Visibility:** Public
- **Members:** Test Admin (owner), Test Staff (staff), Test Member (member)

### 3. Test Events (11 Events Total)

Each event is designed to test a specific eligibility gate or combination of gates:

#### ✅ Event 1: Accessible Public Event
- **Slug:** `test-accessible-event`
- **Purpose:** Baseline - any user can RSVP
- **Eligibility:** ✅ Allowed for random user
- **Expected NextStep:** `RSVP`

#### 📋 Event 2: Event Requires Questionnaire
- **Slug:** `test-event-with-questionnaire`
- **Purpose:** Tests QuestionnaireGate
- **Eligibility:** ❌ Blocked until questionnaire completed
- **Expected NextStep:** `COMPLETE_QUESTIONNAIRE`
- **Expected Reason:** "Questionnaire has not been filled"

#### 👥 Event 3: Members-Only Event
- **Slug:** `test-members-only-event`
- **Purpose:** Tests MembershipGate
- **Event Type:** MEMBERS_ONLY
- **Visibility:** PUBLIC (visible to all, but access restricted)
- **Eligibility:** ❌ Blocked for non-members
- **Expected NextStep:** `BECOME_MEMBER`
- **Expected Reason:** "Only members are allowed"

#### 🔒 Event 4: Private Event (Invitation Required)
- **Slug:** `test-private-event`
- **Purpose:** Tests InvitationGate
- **Event Type:** PRIVATE
- **Visibility:** PUBLIC (visible to all, but access restricted)
- **Eligibility:** ❌ Blocked without invitation
- **Expected NextStep:** `REQUEST_INVITATION`
- **Expected Reason:** "Requires invitation"
- **Note:** Test Member has an invitation (can test difference)

#### 🚫 Event 5: Event at Full Capacity
- **Slug:** `test-full-capacity-event`
- **Purpose:** Tests AvailabilityGate
- **Max Attendees:** 10 (all filled)
- **Eligibility:** ❌ Blocked due to capacity
- **Expected NextStep:** `JOIN_WAITLIST`
- **Expected Reason:** "Event is full"

#### ⏰ Event 6: RSVP Deadline Passed
- **Slug:** `test-rsvp-deadline-passed`
- **Purpose:** Tests RSVPDeadlineGate
- **RSVP Deadline:** Already passed
- **Eligibility:** ❌ Blocked after deadline
- **Expected NextStep:** None
- **Expected Reason:** "The RSVP deadline has passed"

#### 🎟️ Event 7: Tickets Not Yet On Sale
- **Slug:** `test-tickets-not-on-sale`
- **Purpose:** Tests TicketSalesGate
- **Requires Ticket:** Yes
- **Sales Start:** 30 days from now
- **Eligibility:** ❌ Blocked until sales open
- **Expected NextStep:** None
- **Expected Reason:** "Tickets are not currently on sale"

#### 📝 Event 8: Draft Event (Not Yet Open)
- **Slug:** `test-draft-event`
- **Purpose:** Tests EventStatusGate (draft)
- **Status:** DRAFT
- **Eligibility:** ❌ Blocked until opened
- **Expected NextStep:** `WAIT_FOR_EVENT_TO_OPEN`
- **Expected Reason:** "Event is not open"

#### ⏹️ Event 9: Past Event (Finished)
- **Slug:** `test-finished-event`
- **Purpose:** Tests EventStatusGate (finished)
- **End Date:** 7 days ago
- **Status:** CLOSED
- **Eligibility:** ❌ Event already ended
- **Expected NextStep:** None
- **Expected Reason:** "Event has finished"

#### 🎫 Event 10: Event Requires Ticket Purchase
- **Slug:** `test-requires-ticket`
- **Purpose:** Tests ticket requirement (available tickets)
- **Requires Ticket:** Yes
- **Tickets On Sale:** Yes
- **Eligibility:** ✅ Can purchase
- **Expected NextStep:** `PURCHASE_TICKET`

#### 💸 Event 11: Sold Out Event
- **Slug:** `test-sold-out-event`
- **Purpose:** Tests AvailabilityGate + TicketSalesGate
- **Requires Ticket:** Yes
- **Capacity:** 5/5 tickets sold
- **Eligibility:** ❌ Sold out
- **Expected NextStep:** `JOIN_WAITLIST`
- **Expected Reason:** "Sold out"

## Eligibility Gates Tested

This command tests all eligibility gates from `EligibilityService`:

1. ✅ **PrivilegedAccessGate** - Staff/owners bypass restrictions
2. ✅ **EventStatusGate** - Event must be open (not draft/finished)
3. ✅ **InvitationGate** - Private events require invitation
4. ✅ **MembershipGate** - Members-only events require membership
5. ✅ **QuestionnaireGate** - Events can require questionnaire completion
6. ✅ **RSVPDeadlineGate** - RSVP must be before deadline
7. ✅ **AvailabilityGate** - Event must not be full
8. ✅ **TicketSalesGate** - Tickets must be on sale

## Testing Scenarios

### For Random User (No Org Affiliation)

Login as `random.tester@example.com` to test:

1. **Accessible Event** - Should see "RSVP" button
2. **Questionnaire Event** - Should see "Complete Questionnaire" prompt
3. **Members-Only** - Should see "Become Member" prompt
4. **Private Event** - Should see "Request Invitation" prompt
5. **Full Event** - Should see "Join Waitlist" option
6. **RSVP Deadline Passed** - Should see deadline passed message
7. **Tickets Not On Sale** - Should see "not yet on sale" message
8. **Draft Event** - Should see "event not open" message
9. **Finished Event** - Should see "event finished" message
10. **Requires Ticket** - Should see "Purchase Ticket" button
11. **Sold Out** - Should see "Sold Out" with waitlist option

### For Test Member

Login as `test.member@example.com` to test:

- Has access to members-only event (no "Become Member" prompt)
- Has invitation to private event (can access)
- Still blocked by questionnaire gate (unless completed)

### For Test Staff

Login as `test.staff@example.com` to test:

- Should bypass most gates due to staff status
- Can access draft events for preview

### For Test Admin

Login as `test.admin@example.com` to test:

- Full access to all events as owner
- Can manage events

## API Endpoints to Test

### Check Eligibility
```bash
GET /api/events/{event_id}/eligibility/
```

Returns `EventUserEligibility` with:
- `allowed`: boolean
- `reason`: string | null
- `next_step`: NextStep | null
- Questionnaire-related fields if applicable

### Get Event List
```bash
GET /api/events/?organization=eligibility-test-org
```

All events should be visible (visibility=PUBLIC) but with different eligibility statuses.

## Frontend Testing Checklist

Use these events to validate:

- [ ] Correct eligibility status display for each gate
- [ ] Appropriate call-to-action buttons (RSVP, Purchase, Join Waitlist, etc.)
- [ ] Clear messaging about why access is restricted
- [ ] Questionnaire flow (link to questionnaire, completion status)
- [ ] Membership prompts and links
- [ ] Invitation request flow
- [ ] Ticket purchase flow
- [ ] Waitlist join flow
- [ ] Different views for logged-in vs anonymous users
- [ ] Different views for members vs non-members
- [ ] Staff/owner privileged access indicators

## Cleaning Up

To remove all test data:

```bash
# Delete the test organization (cascades to events)
python manage.py shell
>>> from events.models import Organization
>>> Organization.objects.filter(slug='eligibility-test-org').delete()

# Delete test users
>>> from accounts.models import RevelUser
>>> RevelUser.objects.filter(email__contains='@test.com').delete()
>>> RevelUser.objects.filter(email='random.tester@example.com').delete()
```

## Notes

- All events have `visibility=PUBLIC` to ensure they appear in event listings
- Event `type` varies (PUBLIC, MEMBERS_ONLY, PRIVATE) to gate access
- Event names include emoji and descriptive text for easy identification
- Dummy users are created to fill capacity-limited events
- Test member receives an invitation to demonstrate invitation bypass

## Extending This Command

To add more test scenarios:

1. Add event creation in `_create_test_events()`
2. Store in `self.events` dict with descriptive key
3. Create associated ticket tiers in `_create_ticket_tiers()` if needed
4. Add relationships in `_create_relationships()` if needed
5. Update this README with the new scenario

## Related Files

- Event Manager: `src/events/service/event_manager.py`
- Eligibility Gates: `BaseEligibilityGate`, `EligibilityService`
- Models: `src/events/models/`
- Original Bootstrap: `src/events/management/commands/bootstrap_events.py`

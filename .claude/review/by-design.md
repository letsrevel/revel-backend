# By design: revel-backend

Policy-level intended behaviour that reviewers must not report as a finding. Each entry states the behaviour, the boundary (what is still a bug), and a reference. Reviewers and verifiers read this before reporting. Zero findings is a valid outcome; never pad a report.

## Shareable invitation and token links
- behaviour: organization and event invitation tokens (staff, member, attendee) are bearer tokens; any authenticated user holding a valid link can claim it. "Anyone with the link can claim it" is the feature.
- boundary: a claim must still respect global and organization bans and blacklists, expiry, usage limits, the token's scope, and its role ceiling (a link can never grant more than it was issued for, and never owner rights). Claiming must not leak anything beyond what the claim grants. Tokens issued for single use or a single recipient must not be replayable.
- reference: `docs/architecture/permissions.md`; maintainer decision.

## Personal membership invitations
- behaviour: a personal invitation (`OrganizationToken` with `invitee` set) is claimable only by its invitee, but its public preview (`GET /organizations/tokens/{id}`) and `X-Org-Token` view access behave like any membership link, so the invitee can see the organization before accepting. Staff with `manage_members` can see and withdraw pending invitations.
- boundary: a claim by anyone other than the invitee, any route that edits or lists personal invitations as shareable links (`/tokens`), a preview or response that exposes the invitee, or creating a membership before the invitee accepts are still bugs.
- reference: `docs/architecture/member-register.md` (member invitations); maintainer decision 2026-11-18.

## Staff-originated rows are masked
- behaviour: a box-office ticket, a staff-recorded RSVP or a staff-made event invitation shows staff only what staff supplied (the email they typed, the name they typed) unless the account is known to the organization through its own acts (`events/service/holder_visibility.py`); `StaffViewUserSchema` then returns `known: false` with profile fields `null`. Such rows also give no standing for RSVP on behalf, box-office sale by `user_id` or private notes. A holder who later acts becomes known, and their earlier rows unmask.
- boundary: any admin surface, export or staff notification that shows an unknown holder's phone, legal or preferred name, pronouns, bio or picture is a bug, as is a staff-made row counting as standing, or a new operation projecting `MemberUserSchema`/`MinimalRevelUserSchema` without an entry in `test_holder_profile_guard.py`. Members are never masked. A blacklist entry never creates a membership row for a non-member (legacy banned rows from earlier bans still count as known), and a waitlist entry consumed by a staff sale stops conferring standing (accepted, fails closed).
- reference: ADR-0020; maintainer decision 2026-11-19.

## Staff permissions do what they say
- behaviour: a staff member holding a permission (for example `manage_members` or `invite_to_event`) can perform exactly that action in their organization. The organization owner implicitly passes organization permission checks.
- boundary: a staff member without the permission, or acting on another organization's objects, is a bug. Staff must not be able to escalate to owner, grant themselves or others more than they hold, or edit owner-only fields. An OAuth app token must never exceed its granted scopes: the scope gate runs before any owner or staff shortcut.
- reference: `docs/architecture/permissions.md`.

## Permission class plus manual owner check
- behaviour: object-level access is often enforced twice (a permission class and an explicit owner or organization check in the service). The duplication is defense in depth, not an inconsistency. A bare model class passed to `get_object_or_exception()` is still authorized by the permission class attached to the controller.
- boundary: a reachable path where no layer checks object ownership or organization is a bug.
- reference: `docs/architecture/permissions.md`, `docs/architecture/controllers.md`.

## Reusable unsubscribe tokens
- behaviour: unsubscribe links work on every click, from any email, at any time; they are not single-use.
- boundary: a token may only change the notification and suppression preferences of the user it was issued for, and nothing else. It must not be a credential for any other action, and it must not reveal account data.
- reference: `docs/architecture/email-sending.md`, `docs/architecture/notifications.md`.

## Identifiers and field names in eligibility and error responses
- behaviour: eligibility and validation responses include UUIDs, field names, statuses and next-step hints because the frontend needs them to guide the user (for example which questionnaire to fill in).
- boundary: responses must not expose another user's personal data, attendee-only information (address, attendee list, cancellation reasons) to someone who is not attending, or the contents of resources the caller may not see. Stack traces and internal exception detail must not reach the client.
- reference: `docs/architecture/eligibility-pipeline.md`.

## Attendance is ACTIVE or CHECKED_IN
- behaviour: "attending" means a ticket in status ACTIVE or CHECKED_IN. Only attending users see attendee-only information (address, attendee list, attendee-only resources and polls, and so on). A PENDING ticket (for example an unpaid wire transfer) does not unlock it. Notification audiences are deliberately broader and include PENDING buyers so they hear about updates and cancellations. Capacity, seat, discount and check-in counts use their own definitions.
- boundary: any code that unlocks attendee-only data from `status != CANCELLED`, from PENDING, or from ACTIVE alone (which drops checked-in attendees) is a bug; the definition lives in `Ticket.ATTENDING_STATUSES` and `Ticket.objects.attending()`. Conversely, narrowing a notification audience to attending tickets only is a regression.
- reference: `src/events/tests/test_attending_followups_1278.py`; maintainer decision (#1308).

## Venues are public
- behaviour: a venue (name, address, map URL and embed, city, capacity, description) is public information. It is nested in full in every event, event list, calendar, ticket and ticket-tier response, whatever the event's `address_visibility`. That setting governs only the event's own `address` and map fields (`EventDetailSchema.resolve_address` and siblings). An organizer who needs a hidden location uses a custom address instead of a venue; the frontend form says so (letsrevel/revel-frontend#1254).
- boundary: the event's own `address`, `location_maps_url` and `location_maps_embed` must still be gated by `Event.can_user_see_address` wherever they are returned. Admin-only venue data (sectors, seats, layout metadata beyond the public chart) stays behind organization permissions.
- reference: maintainer decision 2026-11-18 (re-confirmed after a scan flagged it).

## Platform fee is not refunded
- behaviour: when a ticket is refunded, Revel keeps its platform fee (the application fee), mirroring how Stripe keeps its processing fee on a refunded charge. This is a business decision, not an oversight.
- boundary: while the fee exists it must be apportioned correctly: mixed-price carts split it pro rata by ticket price with largest-remainder rounding, never evenly. Refund amounts must still never exceed what was paid for the ticket, and the organization's share must be refunded correctly.
- reference: maintainer decision; apportionment bug tracked and fixed in #753.

## Uniform price per cart
- behaviour: batch checkout is built on one price per cart (discounts are delivered as a price override computed from the tier price; line items and refund matching assume it). This is a known design constraint, not a defect.
- boundary: today no path may let two tickets in one cart end up with different effective prices. A change that introduces per-ticket price differences (per-seat-category pricing, partial discounts) must revisit every site that assumes a scalar cart price; flag a diff that does so without handling them.
- reference: `docs/engineering-notes.md` (checkout reserve/session split); maintainer decision.

## Discount uses follow unfinished checkouts
- behaviour: a discount code use is given back when a still-PENDING ticket is cancelled or removed, including an offline ticket the organizer moved back to pending with "unconfirm" and then cancelled (it is unpaid again). Confirmed (paid) tickets that are later cancelled or refunded keep their use.
- boundary: releasing a use twice for one ticket, or releasing for a ticket that never consumed one, is a bug.
- reference: owner decision 2026-11-18.

## `SYSTEM_TESTING` flag
- behaviour: `SYSTEM_TESTING` is an operational toggle for test and CI behaviour, set through the environment.
- boundary: it must not be settable from a request, and production configuration must not enable it.
- reference: `docs/adr/0008-environment-feature-flags.md`.

## LLM prompt-injection defenses
- behaviour: questionnaire answers are sanitized and evaluated with defensive prompting; these defenses were stress-tested and are accepted as they are.
- boundary: report only a concrete, demonstrated bypass that changes an evaluation outcome or leaks data across users, with the input that does it.
- reference: `docs/architecture/questionnaires.md`; maintainer decision.

## `update_db_instance` saves without `update_fields`
- behaviour: `common.utils.update_db_instance` is a deliberately generic helper; the caller decides which fields are written.
- boundary: a caller that feeds it unrestricted client input containing privileged fields is a bug; callers must pass a restricted schema dumped with `exclude_unset=True`.
- reference: `docs/architecture/permissions.md`; maintainer decision.

## Blacklist quick mode by user id
- behaviour: staff can blacklist an account in quick mode by `user_id`. The ids staff see only come from rows created by that account's own interaction with the organization (membership, requests, tickets, RSVPs, invitations and so on), so the route is not an account lookup or enumeration oracle.
- boundary: a route that accepts an arbitrary user id and returns account data (name, email, existence) for an account with no connection to the organization, or a blacklist response that reveals more about the account than staff could already see on those rows, is still a bug.
- reference: issue #1453 (closed); maintainer decision.

## Review scope rules
- Present-day only: a hypothetical future regression ("if someone removes this check") is not a finding.
- Legitimate access: if the "attack" needs access the caller already has, or the impact is trivial or indistinguishable from normal use, it is not a vulnerability.
- Feature requests: missing audit trails, extra logging, soft-delete suggestions, additional confirmation steps, and more throttling on endpoints that are already throttled go to the backlog, not a security report.
- boundary: a missing throttle on an expensive or abusable endpoint (LLM, file scan, geo, mass email) with no throttle at all remains a valid finding.
- reference: maintainer decision.

## Release notes describe effects, not weaknesses
- behaviour: CHANGELOG entries and release notes state the user-facing effect and the API contract of a change (status codes, error codes, fields). They do not use a `### Security` heading for quiet fixes, and do not describe a weakness, its trigger or its mechanics.
- boundary: a missing contract fact that clients or operators need (a new status code, a field change, a required operator step) is a docs finding; the absence of a security label or of exploit detail is not.
- reference: owner decision 2026-11-20.

## Check-in takes the tier lock
- behaviour: check-in locks Payment, then the tier and the ticket with `ticket_service.lock_tier_then_ticket`, like every other ticket writer. Scans of tickets of the same tier therefore run one after another, a few milliseconds each.
- boundary: a network call (Stripe, email, wallet) inside the check-in lock, or a lock held across more than the one short transaction, remains a valid finding.
- reference: owner decision 2026-11-22 (#1482).

## Account deletion: no row lock across the subscription cancel and the referral cleanup
- behaviour: a deletion run with a mark (`accounts.tasks.delete_user_account`) reads the account again immediately before the subscription cancel, immediately before the referral cleanup, and under the account row lock before the erase or delete. The lock is held for the last phase only. A change of the account state that commits after the check of the cancel or of the cleanup, and before that step ends, does not stop that step. A later step is skipped when its own fresh check fails; a check only sees the state at that moment, so a change that is undone before the next check (for example, a stale-deletion requeue whose dispatch fails and puts the old mark back) does not stop the later steps. The lock is not held across these two steps because the cleanup writes statement PDFs (file writes cannot be rolled back) and would contend with the payout disbursement task, and the cancel must commit on its own so that its Stripe call fires.
- boundary: a step that starts after its own check failed, an erase or delete of an account whose state changed before the locked check, or a new destructive step that has no check on a fresh read immediately before it, remains a valid finding.
- reference: owner decision 2026-11-23 (#1476, PR #1509).

## Ban-queued and admin-queued account deletion: a failed dispatch is logged only
- behaviour: an admin delete, "Force full delete" and an email or Telegram ban that clears the mark of a pending deletion queue one deletion run without a mark on commit. The domain ban does not: for an owner that is already inactive it only disarms the owner's OAuth apps and keeps the mark, so the self-service run and the stale-deletion sweep still own that deletion. When that dispatch fails, the code logs `account_erasure_dispatch_failed` (ERROR, user id only), the account stays inactive, and an operator queues the deletion again. There is no durable record and no automatic retry for these runs; the stale-deletion sweep covers self-service marks only. An operator alert on the log event is an infra follow-up.
- boundary: a failed dispatch that is not logged, that leaves the account active, or that gives the access back remains a valid finding.
- reference: owner decision 2026-11-23 (#1476, PR #1509).

## Stored Stripe webhook events keep the payload of an erased buyer
- behaviour: `StripeWebhookEvent.payload` stores each Stripe event as it arrived, also for a buyer whose account is erased, so it can hold that buyer's billing details (name, email, address). It is an access-restricted audit log with no link to the user, it is deleted after `STRIPE_WEBHOOK_EVENT_RETENTION_DAYS` (90 days), and its content matches the billing details that kept payments of kept tickets retain for the same reason. The erasure clears `Payment.raw_response`, not this log.
- boundary: an API, export or staff view that serves these payloads, a retention longer than the setting, or a new copy of buyer details from this log onto another row after the erasure remains a valid finding.
- reference: owner decision 2026-11-23 (#1476, PR #1509); the privacy policy update for #1476 names it.

## Automatic member-register link on a verified email
- behaviour: when an account verifies an email, every unlinked, started, non-revoked member-register entry with the same email (`iexact`) is linked to it, and an open STANDARD entry gives an ACTIVE membership. Nobody confirms the link. Staff get `staff_member_record_auto_linked`; the person gets `member_record_linked` (one per organization, only when the link leaves an ACTIVE membership; it replaces `membership_granted` on this path). This migrates existing members.
- boundary: a link for a guest, an unverified email, a revoked or merged entry, a future entry, or a banned or blacklisted person is a valid finding. So is a link that gives an ACTIVE membership with no notification to the person. A link that touches only closed or TEMPORARY entries sends nothing to the person by design.
- reference: owner decision 2026-11-21 (#1495).

## Docs state functional limits
- behaviour: docs and docstrings may state functional limits of a feature (what it does not cover, what is capped, what a caller must do).
- boundary: they never describe an unfixed weakness or how to trigger it. A doc that does is a finding; a doc that states an honest functional limit is not.
- reference: owner decision 2026-11-21 (docs-3).

## First verification keeps the signup password
- behaviour: someone can sign up with another person's email address. If that person clicks the verification link, the account becomes verified and keeps the password of the first signer, when nobody else signed up with the address in the meantime. When the signup was contested (`signup_contested_at`), verifying restarts the account (`adopt_unverified_account` drops the earlier credentials and resets the profile) and sends a password-reset email. The verification email has a "didn't sign up? delete the account" link.
- boundary: the account stays unverified until the link is used, and the link never signs anyone in. Accepted as it is. A path that skips email verification, or that gives the first signer access before the verification, is still a bug.
- reference: owner decision 2026-11-21 (U2).

## Member-register entries link on email verification
- behaviour: when an account verifies its email, register entries with that email link to it automatically, and an open STANDARD entry gives an ACTIVE membership. This is intended: it migrates existing members into accounts. The person is notified (#1495) only when the link leaves an ACTIVE membership; a link that touches only closed or TEMPORARY entries sends the person nothing (see "Automatic member-register link on a verified email").
- boundary: a membership created for an invitation before the invitee accepts is still a bug.
- reference: owner decision 2026-11-21; #1495; `docs/architecture/member-register.md`.

## Series pass sweep and waitlist offers share the free seats
- behaviour: the reconcile sweep (`events.reconcile_series_pass_tickets`, every 15 minutes) issues a missing ticket to an ACTIVE pass holder when a seat is free, also when that seat became free after a cancellation and the waitlist has people on it. The sweep and `process_waitlist_for_event` count the same free seats, and the one that runs first gets the seat. There is no fixed priority: a holder can get a freed seat before a waitlist offer exists for it, and a waitlist offer that is created first holds the seat and the sweep then skips the holder.
- boundary: the sweep checks tier and event capacity under the locks (pending waitlist offers count as taken seats) and the holder's blacklist status, and it never issues a ticket again that the organizer cancelled. A sweep that takes a seat that a pending waitlist offer holds, or that goes over capacity, is a valid finding.
- reference: owner decision 2026-11-26 (#1536, PR #1567).

## A broken cross-field rule blocks every save of the row
- behaviour: `TimeStampedModel.save()` with `update_fields` validates only the written fields with `clean_fields`, but `clean()`, the unique checks and the constraints run on the whole row. So when an earlier edit left a cross-field rule broken (for example `waitlist_cutoff_date` after `start`), every save of that row fails, a status change and a cancel too, until someone fixes the field. The API 400 and the admin message name the field.
- boundary: a save that fails because of an unrelated single field (a `clean_fields` check of a field that it does not write, or a stored image file) is a valid finding. So is an error that does not name the field.
- reference: owner decision 2026-11-26 (#1563, options B + A + D).

# Member Register & Day Memberships

The **member register** is an organization's legal log of membership periods. Each row (`MembershipRecord`) says that one person was a member of one organization, at one tier, from one date to another. It is separate from the access row (`OrganizationMember`), which only says what a person may do *now*. Records outlive their account, their tier and the questionnaire submission that justified them, so an organization can answer "who was a member on date X, and what did they accept?" years later.

This page covers the data model, how records are created and closed, linking account-less entries to accounts, **day memberships** (temporary tiers granted by tickets), private organizer notes, the admin API and the background tasks.

!!! note "Two layers, one direction"
    `OrganizationMember` is the source of truth for access; the register **mirrors** it. Changing a member's status or tier through [membership subscriptions](membership-subscriptions.md), applications, tokens or staff actions updates the access row, and a single `post_save` receiver opens or closes the matching record. Staff never edit the register to change access; edits to a record that backs a live member are refused or routed through the member.

---

## Data model

All in `src/events/models/membership_record.py` (migration `0140_membershiprecord`; `0141_backfill_membership_records` backfills existing members).

### `MembershipRecord`

| Field | Notes |
|---|---|
| `organization` | FK, CASCADE. |
| `user` | FK, **SET_NULL**, nullable. Cleared when the person deletes their account, or for entries that were never linked. A record without a user grants nothing. |
| `full_name`, `email` | **Snapshots**, not live references: the legal name (first + last, else display name, truncated to 255) and the address at the time. `email` is a plain `CharField` so saving never rejects a stored address. They are what remains after the account is gone. |
| `kind` | `standard` (a normal membership) or `temporary` (a [day membership](#day-memberships)). |
| `tier`, `tier_name` | FK (SET_NULL) plus a name snapshot, so deleting or renaming a tier does not rewrite history. |
| `valid_from`, `valid_until` | Half-open period. `valid_until` NULL means open. A check constraint forbids `valid_until < valid_from`. |
| `ended_reason` | `cancelled`, `expelled` (banned), `removed`, `revoked` (a day membership withdrawn) or `tier_changed`. |
| `source` | Why the record exists: `application`, `staff`, `token`, `subscription`, `ticket`, `manual`, `import` or `backfill`. |
| `ticket`, `event`, `subscription` | Provenance FKs (SET_NULL). `ticket` is unique when set (one day record per ticket). |
| `questionnaire_submission`, `accepted_at`, `questionnaire_name` | Acceptance evidence: the submission the person made, when, and the questionnaire's name, kept even if the submission is deleted. |
| `created_by` | The staff member for manual entries. |
| `invite_token`, `invited_at`, `duplicate_claimant`, `linked_via`, `linked_at` | Linking state, see [Linking](#linking-entries-to-accounts). `invite_token` is never serialized. |
| `merged_into`, `merge_prior_valid_from` | Reconciliation state, see [Merging](#merging-and-unlinking). |

Constraints: `unique_open_standard_record_per_user` (at most one open STANDARD record per `(organization, user)`), `membership_record_valid_range`, `unique_membership_record_ticket`. Query helpers on `MembershipRecordQuerySet`: `open()`, `unlinked()`, `unmerged()`, `active_on(instant)` (non-revoked, unmerged, covering the instant), `active_during(start, end)` (overlaps a window; zero-length periods cover nothing) and `ended()`.

### `MembershipTier.kind`

`MembershipTierKind` is `STANDARD` or `TEMPORARY`. Temporary tiers exist only to be granted by tickets, and the code treats them as a different species:

- No members: `OrganizationMember.clean()` rejects them, and `MembershipTier.objects.standard()` is what member-facing and member-management code queries.
- No applications: the `/join-eligibility` and `/apply` controllers resolve tiers through `standard()`, so a temporary tier id is a 404; `OrganizationMembershipRequest.clean()` rejects them too.
- No plans: `MembershipSubscriptionPlan.clean()` rejects them.
- No organization tokens: `OrganizationToken.clean()` ("A temporary tier cannot be granted by a token").
- No membership questionnaire or approval policy on the tier itself.
- Not usable to gate or exclude buyers: `TicketTier.restricted_to_membership_tiers` and `excluded_membership_tiers` reject them, since nobody holds one on an access row.
- Not targetable by announcements (`announcement_service._resolve_target_tiers`) or by poll tiers (`poll_service` queries `standard()`).

### `TicketTier.grants_membership_tier`

A ticket tier may name a temporary tier of the same organization to grant. `TicketTier.clean()` enforces: the granted tier is `TEMPORARY` and in the event's organization; `purchasable_by` is **not** `MEMBERS` or `INVITED_AND_MEMBERS` (a membership cannot be gated behind membership); and `max_tickets_per_user == 1` (one ticket, one day membership, per person). Tiers that grant a membership are also not synced to external listing platforms, because a sale there creates no Revel ticket (see [ADR-0017](../adr/0017-platform-listings-provider-protocol.md)).

---

## How records are created and closed

| Source | Mechanism |
|---|---|
| **Access-row changes** | `member_register_service.mirror_member`, called from the `OrganizationMember` `post_save` receiver. ACTIVE and PAUSED keep exactly one open STANDARD record (a tier change closes it as `tier_changed` and opens a successor that inherits source, evidence and subscription); CANCELLED closes it as `cancelled`; BANNED closes it as `expelled`. `AWAITING_PAYMENT` opens nothing (the person is not a member until the first payment); if an entry was open, it closes as `cancelled`. |
| **Describing why** | Callers wrap the write in `member_register_service.record_source(source, questionnaire_submission=...)`: `application` (with the applicant's submission as evidence), `token`, `subscription`, `staff`. With no block the source defaults to `staff`. `suppress_mirror()` skips mirroring inside a block. |
| **Tier deletion** | Deleting a tier SET_NULLs members with a bulk update that fires no signal, so `reopen_for_tier_deletion` closes and reopens the affected records without a tier, in the same transaction. |
| **Manual entries** | Staff add account-less STANDARD entries by hand (`source=manual`), e.g. members who predate the platform. |
| **Day memberships** | Created from tickets by `day_membership_service` (`source=ticket`). |
| **Backfill** | `0141_backfill_membership_records` gave every existing `(organization, user)` pair a record (`source=backfill`, starting at the member's `created_at`). Bans placed on strangers (no tier, no application, no subscription) are skipped. It is idempotent. |

When the register is about to open a *new* STANDARD record for an access row, it first tries `adopt_unlinked_record`: an account-less open entry with the same verified email (`iexact`, never a guest or unverified address, never future-dated) is linked instead, so migrating members do not get duplicates. Staff are notified (`STAFF_MEMBER_RECORD_AUTO_LINKED`).

Records that back a live member are owned by the mirror. `member_register_admin_service` refuses edits that would fight it: the tier of a linked open membership changes through member management (409), a linked open membership is ended by cancelling the member (`end_record` does that and then stamps the record), a linked ended membership cannot be reopened, and a linked open one cannot start in the future. Account-less records are fully editable, as a hand-corrected legal log.

---

## Linking entries to accounts

An account-less entry grants nothing. Linking sets `MembershipRecord.user` and makes the access row agree (`member_register_linking.link_record`). Lock order is user, then access row, then record (the mirror locks member then record).

Three ways in:

1. **Invitation.** Staff send a single-use claim link by email (`POST .../invite`, one entry; `POST .../invite-all`, every open, unlinked, never-invited STANDARD entry with an email that has already started). The email is a direct templated message (`events.send_member_register_invite`), not a notification, because the notification system needs a recipient account. A resend invalidates the previous link; editing the email of an invited entry kills its token. The public pages are `GET /member-register-invites/{token}` (preview, unauthenticated, throttled) and `POST /member-register-invites/{token}/claim` (any logged-in non-guest account, whatever its email).
2. **Verified-email match.** When a user verifies their email, `events.auto_link_member_records` links every unlinked entry whose email equals it (`iexact`, started, not revoked). `linked_via=auto_email`; staff are notified.
3. **Application approval.** `GET /membership-requests/{id}/register-matches` lists unlinked entries that may be the applicant (by email, name or an earlier collision claim); staff verify, then approve with the entry linked.

Collision rules, applied in order by `link_record`: an already linked, merged, revoked or stale-token entry reads as "already linked"; an open STANDARD entry that has not started is `not_started` (409); a banned or hard-blacklisted claimant is refused; a claimant who already holds an open STANDARD entry in the organization is `duplicate` (409) and the entry records `duplicate_claimant` for staff to reconcile. Closed and TEMPORARY entries grant nothing, so linking them touches no access row. The claim endpoints return failures as values rather than exceptions, so the request transaction does not roll back the `duplicate_claimant` flag.

An account-less record's private note moves onto the user's note when the entry is linked (see [Private organizer notes](#private-organizer-notes)).

### Merging and unlinking

`POST .../merge` absorbs an account-less entry (`absorbed_record_id`) into a survivor:

- **Open absorbed entry** into an open STANDARD survivor: the absorbed entry keeps its own dates, stays for audit (`merged_into` set) and counts nowhere; the **survivor keeps its own `valid_from`** (a merge never moves it) and its tier.
- **Closed or TEMPORARY absorbed entry** into a survivor with an account: it is attached to that account (`linked_via=merge`).
- Anything else is a 400; an entry that has absorbed others cannot be merged, and a survivor with absorbed entries cannot be deleted until they are unlinked.

`POST .../unlink` reverses a merge, or a link: unlinking an open linked entry cancels the access row it granted (a reversed link takes the membership away); unlinking a closed or temporary one only removes the account. `GET .../possible-duplicates` lists unlinked entries that may be the same person as a linked open one, with the reasons (`email`, `name`, `claim`).

---

## Day memberships

A **day membership** is a TEMPORARY record granted by a ticket whose tier has `grants_membership_tier` set (`src/events/service/day_membership_service.py`). It exists for the organization's register and for the door scan; it never creates an `OrganizationMember`, so it carries **no platform privileges** (no member-only visibility, no member prices).

- **Grant.** When a ticket becomes attending (ACTIVE or CHECKED_IN), `on_ticket_status_change` (from the `Ticket` `post_save` receiver) calls `_grant`: one TEMPORARY record per ticket, `valid_from = event.start`, `valid_until = max(event.end, event.start)`, `source=ticket`, `ticket` and `event` set. Bulk writers skip signals, so they call `on_tickets_issued` explicitly: batch ticket checkout (`batch_ticket_service/tickets.py`), series-pass issuance and offline pass confirmation (`series_pass_service.py`). The call must precede ticket numbering (lock order). Enabling `grants_membership_tier` on a tier with tickets already sold grants nothing retroactively.
- **Acceptance evidence.** The record stores the newest admission questionnaire submission the event's admission gate would accept (`_admission_evidence`: published, ready, not rejected, not pending review when evaluation is required, not older than `max_submission_age`, a `per_event` one made for this event; for pass tickets a series-linked `per_event` one made for any event of the series). `accepted_at` and `questionnaire_name` are snapshotted with it.
- **Bans.** A banned person is granted nothing (checked inside the write transaction), and a ban revokes upcoming day records (`revoke_upcoming_for_ban`); ongoing, past and checked-in ones stay.
- **Revoke.** A ticket leaving the attending statuses (cancel, refund) revokes its record (`ended_reason=revoked`) if the event has not started and the person never checked in; a deleted event revokes its records. A revoked record is reopened if the ticket becomes attending again.
- **Date changes.** Editing the event's dates before it ends re-syncs the records (`resync_event_window`); once the event ends the record is frozen.
- **Check-in.** `check_in_ticket` and the member-card scan attach a `membership` block (`CheckInMembershipSchema`: `kind`, `tier_name`, `valid_until`) from `membership_summary`: an open STANDARD record wins, else the ticket's day record. Admin ticket schemas expose the record the ticket produced as `membership_record` (including revoked ones, with `ended_reason`).

---

## Private organizer notes

`OrganizationUserNote` (`src/events/models/user_note.py`, migration `0139`) is a private, editable note an organization keeps about a person. It targets exactly one of `user` or `record` (check constraint), so account-less register entries can carry notes too. It is keyed on `(organization, user)` rather than on the member row, so it survives removal and exists for attendees who were never members.

- **API** (`controllers/organization_admin/user_notes.py`, `manage_members`, session auth only): `GET | PUT | DELETE /organization-admin/{slug}/users/{user_id}/note`, and the same under `/member-register/{record_id}/note`. A linked entry proxies to the user's note; a merged entry has no note of its own (400). The body is sanitized Markdown, at most 5000 unescaped characters, never empty. A `PUT` for a user with no relationship to the organization (member, ticket holder or pass holder) is a 404, so the endpoint is not an existence oracle. `UserNoteSchema` returns `body`, `updated_at`, `updated_by_id` and `updated_by_name` (null once the editor's account is gone).
- **Visibility.** Never on member- or attendee-facing schemas, exports, notifications or OAuth app-token responses. Staff see it as `private_note` on member lists and register entries (`has_note`). Door staff with `check_in_attendees` see it **read-only** in check-in and member-scan responses (`include_note`, disabled when the caller uses an OAuth app token). It is excluded from the GDPR export.
- **Moves.** Linking an entry moves its note onto the user's note, appending with a dated separator and capping at the maximum length when both exist (`user_note_transfer`).

---

## Admin API

Mounted under `/organization-admin/{slug}`, all requiring `manage_members` and **session authentication only**: no OAuth scope's consent label promises the legal register (names and emails of people with and without accounts), so it is not on the app-token surface.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/member-register` | List entries, newest first; searchable on name and email. Filters: `kind`, `tier_id`, `user_id`, `include_merged`, `merged_into`, `is_open`, `has_account`, `ended`, `active_on`, `active_at`. `active_on` is a calendar day in the organization's timezone (overlap); `active_at` is an exact instant. Passing **both** is a 400. Each row carries `has_note`, `is_current`, the linked account's `member_status` and live subscription summary, invite status and `can_invite` / `invite_blocked_reason` |
| `POST` | `/member-register` | Add a manual account-less entry (`full_name`, `email`, `tier_id`, `valid_from`, `valid_until`) |
| `GET` / `PATCH` | `/member-register/{id}` | Read / edit. Linked live memberships are edited through member management (409) |
| `POST` | `/member-register/{id}/end` | End an entry (`cancelled` or `removed`); for a linked live membership this cancels the member |
| `DELETE` | `/member-register/{id}` | Hard-delete. **Owner only**; refused for the open entry of a live member and for a survivor with merged entries |
| `POST` | `/member-register/{id}/invite` | Send or resend the claim link |
| `POST` | `/member-register/invite-all` | Invite every eligible entry (202, `{invited}`). Throttled to **5 per day** per user (`MemberRegisterBulkInviteThrottle`) |
| `POST` | `/member-register/{id}/merge` | Merge an account-less entry into this one |
| `POST` | `/member-register/{id}/unlink` | Reverse a link or merge |
| `GET` | `/member-register/possible-duplicates` | Unlinked entries that may duplicate a linked open one |
| `GET` | `/membership-requests/{id}/register-matches` | Unlinked entries that may be this applicant |
| `POST` | `/member-register/export` | Start an xlsx export (`active_on`, `include_merged`); 202 with a `FileExport` to poll. Export-throttled; see [Exports](exports.md) |
| `GET` | `/users/lookup?email=` | Resolve an **exact** verified email to a `user_id` plus their membership status, open register entry, pending request and pending invitation (`pending_invitation_id`), for the member-invitation flow. It returns a display name, never contact details. Every non-match (no account, unverified, guest, inactive) returns the same 404; throttled to 30 per hour per user because it is an account-existence oracle |
| `GET` | `/member-invitations` | Pending personal invitations (not expired, not yet accepted): invitee display name, tier, expiry |
| `POST` | `/member-invitations` | Invite an existing account (`user_id`, `tier_id`). Creates no membership and no register entry: the account gets a personal link (`membership_invitation_received`) and becomes a member only by accepting it via `/organizations/claim-invitation/{token}` (register source `token`). Valid 14 days; one invitation per account and organization, so inviting again renews it. 400 `already_member` if the account has a membership row in any status |
| `DELETE` | `/member-invitations/{id}` | Withdraw a pending invitation |

Single-entry invites are bound by the controller's write throttle, and the public preview by an anonymous 20/min throttle. The literal routes (`invite-all`, `possible-duplicates`, `export`) are registered before the `{id}` routes.

Tiers: `GET /membership-tiers` accepts `?kind=standard|temporary`, and tier schemas carry `kind`.

---

## Background tasks

| Task | Trigger | Purpose |
|---|---|---|
| `events.send_member_register_invite` | On commit after an invite | Emails the current claim link (only the record id travels through the broker; skipped if the record is gone, linked or has no token) |
| `events.auto_link_member_records` | On commit after email verification | Links entries matching the verified email |
| `events.tasks.generate_member_register_export_task` | `POST /member-register/export` | Builds the xlsx |

---

## Related reading

- [Membership Subscriptions](membership-subscriptions.md) — the subscription state machine that drives access rows (and, through them, register records), including the offline join flow.
- [Membership Eligibility](membership-eligibility.md) — applications, and why temporary tiers cannot be applied for.
- [Eligibility Pipeline](eligibility-pipeline.md) — the membership access predicates and tier-level purchase rules.
- [Series Passes](series-passes.md) — pass tickets also grant day memberships.
- [Permissions](permissions.md) — `manage_members` and `check_in_attendees`.
- [Exports](exports.md) — the xlsx export pipeline.

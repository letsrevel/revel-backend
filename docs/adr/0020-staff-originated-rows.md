# ADR-0020: Staff-originated rows show only what staff supplied

## Status

Accepted (2026-11-19)

## Context

Staff can create rows that point at someone's account without that person doing anything: a
box-office sale or comp to an email address, an event invitation by email, an RSVP recorded on
someone's behalf. Admin views project the linked account through these rows, and some staff
actions (RSVP on behalf, box-office sale by `user_id`, private notes) check whether the account
already has a connection to the organization.

If a staff-made row counted as a connection and carried the account's live profile, staff would
learn profile data (phone, legal name, pronouns, bio, picture) of a person who never interacted
with the organization, only because staff typed their email address.

## Decision

Staff-originated rows show staff only what staff supplied, and confer nothing. Profile data and
standing come only from acts of the account holder.

- One predicate, `events/service/holder_visibility.py` (`known_holder_q`), decides whether an
  account is *known* to an organization. It counts ownership, staff and membership rows,
  membership applications, linked register entries, tickets not issued by the box office (a
  legacy ticket with no recorded source counts), the holder's own RSVPs (`EventRSVP.source`),
  series passes, waitlist entries, event invitation requests and questionnaire submissions. A day
  membership inherits its ticket's origin, and a register entry linked by a staff merge does not count.
  Box-office tickets, staff RSVPs, event invitations and waitlist offers do not count. Only
  attending tickets (active or checked in) count, so a pending checkout confers nothing. A
  blacklist entry never creates an access row for a non-member: it flips an existing member's row to
  banned (again on commit, to catch a membership created concurrently) and leaves everyone else
  without one. Enforcement reads the blacklist itself (`get_hard_blacklisted_user_ids` for bulk
  checks, `check_user_hard_blacklisted` per user), including the day-membership grants, which also
  revoke upcoming day memberships directly, and the follower new-event notifications. Manual entries
  link an account only by an exact verified-email match; phone and telegram do not link, because
  typing an identifier is not proof of who holds it. Unbanning a banned member sets the row to
  cancelled, as before. Residual: banned rows created by earlier bans are indistinguishable from
  former members and still count as known. A waitlist entry consumed by a staff sale (claim
  or the ticket signal) stops conferring standing; this is accepted and fails closed. The default
  `has_relationship` gate (staff add, subscription create) applies the same register-entry rule.
- Admin surfaces whose subject may be unknown return `StaffViewUserSchema`: `known` plus the
  account's profile when known; when not, only the email (the address staff typed) and the name
  staff typed, every other field `null`. Members are always known, so never masked. A blacklist entry
  copies the account's profile (quick mode) or shows its name only for a known holder; otherwise it
  keeps the email and what staff typed. The staff view of
  the attendee list and the dietary and pronoun aggregates leave unknown holders out; the attendee
  export, staff notifications, the register snapshot of a box-office day membership and the default
  invitation greeting use what staff supplied (ticket name, email).
- Staff actions on an account (RSVP on behalf, box-office sale by `user_id`, private notes) require
  a known, active holder; merging a note-bearing register entry into an account moves the note
  only when that account is a known holder.
- A CI guard (`test_holder_profile_guard.py`) pins every API operation that projects the full user
  profile schemas, each with the reason its subject is the holder or someone who acted. A new
  operation fails the guard until it is classified or switched to the masked schema.

## Consequences

- The frontend handles nullable profile fields and `known` on ticket, check-in, RSVP and
  invitation responses.
- Ticket, RSVP and invitation searches no longer match the account's legal or preferred name
  (a match would reveal names the masked rows hide); they still match the email, the ticket name
  and the invitation message.
- A person who later acts (buys a ticket, RSVPs, applies) becomes known, and their earlier
  staff-made rows are unmasked from then on.
- Out of scope: a per-ticket contact snapshot, and an acknowledgement step that unmasks a holder.
  The Django admin (platform staff) is unaffected.

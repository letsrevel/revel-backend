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
  membership inherits its ticket's origin. Box-office tickets, staff RSVPs, event invitations and
  waitlist offers do not count.
- Admin surfaces whose subject may be unknown return `StaffViewUserSchema`: `known` plus the
  account's profile when known; when not, only the email (the address staff typed) and the name
  staff typed, every other field `null`. Members are always known, so never masked.
- Staff actions on an account (RSVP on behalf, box-office sale by `user_id`, private notes) require
  a known holder.
- A CI guard (`test_holder_profile_guard.py`) pins every API operation that projects the full user
  profile schemas, each with the reason its subject is the holder or someone who acted. A new
  operation fails the guard until it is classified or switched to the masked schema.

## Consequences

- The frontend handles nullable profile fields and `known` on ticket, check-in, RSVP and
  invitation responses.
- Ticket search no longer matches the account's legal or preferred name (it would reveal names
  the masked rows hide); it still matches the ticket name and email.
- A person who later acts (buys a ticket, RSVPs, applies) becomes known, and their earlier
  staff-made rows are unmasked from then on.
- Out of scope: a per-ticket contact snapshot, and an acknowledgement step that unmasks a holder.
  The Django admin (platform staff) is unaffected.

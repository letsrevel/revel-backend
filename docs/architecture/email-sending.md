# Email Sending & Opt-Out Policy

Who Revel emails, from which address, and what an unsubscribe does. The policy lives in
`notifications/enums.py` (the type sets), `notifications/service/email_policy.py` (who may be
emailed), `notifications/service/org_sender.py` (sender identity and headers) and
`notifications/service/unsubscribe.py` (tokens and one-click). Operator setup is in
[Tiers & Configuration → Email](../self-hosting/tiers.md#email).

## Sender identity

Mail falls into two groups.

**Organization mail** (`ORG_SENDER_TYPES`) is mail an organization writes or pushes to people:

- `org_announcement`, `invitation_received`
- `new_event_from_followed_org`, `new_event_from_followed_series`
- `event_open`, `event_updated`, `event_cancelled`, `event_reminder`
- invitations to addresses that have no account yet (pending invitations)

It goes out as `"<Org name> via Revel" <org-slug@ORG_EMAIL_DOMAIN>`, where the organization comes
from the notification's `organization_id`, or else from its `event_id`. `ORG_EMAIL_DOMAIN` falls
back to the domain of `DEFAULT_FROM_EMAIL` when unset. A separate sending subdomain keeps the
reputation of organization bulk mail apart from account mail, so one organization's complaint
spike can't push password resets into spam.

- **Reply-To** is the organization's contact email, and only once that address is verified.
  Otherwise there is no Reply-To.
- **Role-name slugs**: an organization whose slug is an RFC 2142 or role mailbox name (`abuse`,
  `postmaster`, `hostmaster`, `webmaster`, `security`, `noreply`, `no-reply`, `support`, `admin`,
  `root`, `mailer-daemon`, `info`, `billing`, `revel`) sends from `DEFAULT_FROM_EMAIL` instead, so
  it can't impersonate a platform mailbox.
- **Headers**: `List-Unsubscribe` (HTTPS only, no `mailto:`) with `List-Unsubscribe-Post:
  List-Unsubscribe=One-Click` (RFC 8058), `Feedback-ID: <slug>:<type>:revel`, and
  `X-Mailin-custom` carrying the delivery (or invitation) and organization ids so provider
  webhooks can be traced back.
- If the organization can't be resolved, the mail falls back to the system sender without
  `List-Unsubscribe`.

**Everything else** (verification, password reset, tickets, payments, staff notifications,
digests) is sent from `DEFAULT_FROM_EMAIL`. Attendee invoices keep their existing
`"<billing name>" <org-slug@<apex domain>>` sender on the apex domain, not `ORG_EMAIL_DOMAIN`.
Their Reply-To follows the same rule as organization mail (verified contact email, else none). The
organization's copy is BCC'd to its billing email (settable by the owner only), else to the
verified contact email, else nobody.

## Pending invitation cap

An invitation to an address with no Revel account (a pending invitation) is cold mail: the
recipient never signed up for Revel. That is the biggest spam-complaint risk on the organization
sending domain, so each organization has a daily budget of them, `PENDING_INVITATION_DAILY_CAP`
(default `200`, `0` means unlimited).

- The cap is checked when the invitations are created (`POST /event-admin/{event_id}/invitations`,
  `events/service/invitation_service.py`). A request that would go over the remaining budget is
  rejected whole with a `400` that says how many it would email, how many are left and the limit.
  Nothing is created.
- Only addresses that would create a new pending invitation count. Addresses of existing users
  (they get a regular invitation) and addresses already pending for that event don't. Duplicates
  in one request count once. Addresses that opted out or are suppressed still count.
- The budget is shared by all events of the organization and resets at midnight UTC. It is never
  refunded: deleting a pending invitation and creating it again spends budget again.
- The counter lives in the cache (Redis), keyed `invite-cap:<org id>:<YYYYMMDD>`. It is not turned
  off by `DISABLE_THROTTLING`.
- A request can list at most 500 addresses (`422` above that).

## Mail users can't opt out of

`MANDATORY_TYPES` bypass silence-all, the email switch, per-type disables and the digest cadence.
They always go out immediately by in-app and email, plus any other channel the user enabled.
The one exception is a [suppressed address](#suppression-list): the provider would drop that
email anyway, so the email delivery is marked failed and the in-app notification still arrives.


| Types | Why |
|---|---|
| `TRANSACTIONAL_TYPES`: `payment_confirmation`, `ticket_created`, `ticket_cancelled`, `ticket_refunded`, and the subscription lifecycle types (`subscription_renewal_succeeded`, `subscription_payment_failed`, `subscription_expired`, `subscription_cancellation_confirmed`, `subscription_revival_checkout`) | Receipts and money or access changes people need to act on |
| `account_banned` | The user must learn why they lost access |
| `system_announcement` | Terms of service, privacy policy and platform-wide notices |

!!! danger "`system_announcement` is never promotional"
    Because nobody can opt out of it, `system_announcement` is reserved for Terms of Service,
    privacy and platform-wide operational notices. Newsletters, feature promotion or anything
    marketing-like must use a type users can switch off. Sending promotional content through it
    breaks users' opt-out and anti-spam law.

## What the user controls

Preferences are resolved in one place, `NotificationPreference.get_channels_for_notification_type`,
which the dispatcher, the email channel and the digest all use.

- **Silence all** (`silence_all_notifications`) stops every non-mandatory notification on every
  channel, in-app included. Mandatory types still arrive.
- **Email channel off** stops email for every non-mandatory type.
- **Per-type toggles** disable one type, or pin its channels.
- **Unsubscribe page** (the footer link, `{frontend}/unsubscribe?token=…`): the user edits the
  global fields (silence, channels, digest) and only the fields they submit change. It no longer
  copies the submitted channels onto every type, which used to make a later re-enable ineffective.
  Migration `0028_unpin_unsubscribe_overrides` undoes those old per-type pins.
- **Digest**: on an hourly, daily or weekly digest, non-mandatory types create the in-app
  notification now and their email waits for the digest. The digest only contains types the user
  may still be emailed, and skips users who silenced everything, turned email off, or whose
  address is suppressed. Mandatory types never wait for the digest.

Unsubscribe tokens last `UNSUBSCRIBE_TOKEN_LIFETIME_DAYS` (default 3650, about ten years), because
the links must keep working for as long as the mail sits in a mailbox. A token stops working when
the account's email address changes.

## One-click unsubscribe

`List-Unsubscribe` points at `/api/notification-preferences/one-click?token=…`. A mailbox
provider's `POST` applies the unsubscribe without login or throttling, and repeats are no-ops. A
browser `GET` only redirects to the frontend unsubscribe page and changes nothing.

| Mail | One-click effect |
|---|---|
| `org_announcement` | Mutes that organization's announcements (all channels) |
| Other organization mail (event updates, reminders, invitations, new events) | Turns email off for that type; other channels keep it |
| Digest | Turns email off and sets the digest back to immediate |
| Invitation to an address with no account | Suppresses the address for invitations from any organization on this instance |

The invitation opt-out only blocks invitations to addresses without an account. If that person
later registers, they still get their tickets and other mail.

## Per-organization announcement mute

`NotificationPreference.muted_organizations` stops one organization's `org_announcement`s on every
channel. Tickets, event updates and other mail from that organization still arrive. The mute is
applied where announcements are delivered, so immediate, scheduled and resend-to-new-attendees
sends all honour it, and the recipient count excludes muted users.

- `PUT` / `DELETE /api/notification-preferences/muted-organizations/{organization_id}` mute and
  unmute; `muted_organization_ids` is returned with the preferences.
- The follow API's `notify_announcements` is a facade over the same mute: `false` mutes, `true`
  unmutes, and a new follow never removes an existing mute. The old
  `OrganizationFollow.notify_announcements` column is no longer read; migration
  `0029_copy_follow_announcement_mutes` copied existing opt-outs into the mute.
- The GDPR export lists muted organizations by id, name and slug.

## Suppression list

`EmailSuppression` holds one row per normalized address that Revel must not email:

| Reason | Source |
|---|---|
| `complaint` | Provider webhook (spam report) |
| `hard_bounce`, `invalid`, `blocked` | Provider webhook |
| `invitation_opt_out` | Recipient, via the invitation opt-out |

A stronger reason overwrites a weaker one (complaint > bounce/invalid/blocked > invitation
opt-out); replays of the same event change nothing.

- **What it blocks**: notification email and digests to users (invitation opt-outs excepted), and
  invitation email to addresses without an account (all reasons). A blocked notification delivery
  is marked `FAILED` with `metadata.suppression_reason` and is never retried. Account mail sent
  outside the notification system (verification, password reset) is not checked.
- **Feeding it**: the provider webhook at `POST /api/email-events/brevo`, inert unless
  `EMAIL_WEBHOOK_SECRET` is set (see [setup](../self-hosting/tiers.md#bounce-and-complaint-webhook)).
  Complaints keep the delivery `SENT` and set `metadata.complained`; bounces mark it `FAILED`.
- **Telling the user**: `GET /api/notification-preferences` returns
  `email_suppression: {"reason": ..., "since": ...}` (or `null`) for the user's current address.
  `since` is when the row last changed. Invitation opt-outs, the provider detail, the source and
  the organization are never exposed. It is deliberately not on `/me`, whose schema is embedded in
  JWTs and readable by OAuth apps. The GDPR export carries the same `email_suppression` section.
- **Clearing it**: there is no self-service clear; it is a support action with two steps:
    1. delete the row in the admin (**Email Suppressions**), **and**
    2. unblock the contact in Brevo (**Transactional → Blocked contacts**). If you skip this, the
       next send is blocked again, the webhook reports it, and the row comes back.

    Alternatively, the user can change their email address. Suppression is keyed by address, so
    the new address is unaffected. A `+alias` of the same mailbox does **not** help: normalization
    strips `+tags` (and Gmail dots), so it matches the same row. Changing the email doesn't clear
    the old address either; the user simply stops matching it. A user whose address bounced before
    they ever verified it can't get far enough in the app to see the notice, so they reach you as a
    support case.
- **Abuse triage**: filter the admin by reason "Spam complaint" and organization to count
  complaints per organization. Rows keep the organization whose mail triggered them.
- **Retention**: rows have no link to a user account and survive account deletion, so a deleted
  and re-registered address still has its bounce, complaint or opt-out honoured.

!!! warning "Gmail complaints don't reach the webhook"
    Gmail doesn't send feedback-loop reports to ESPs, so Gmail spam complaints never arrive as
    `spam` events. Watch the sending domain in Google Postmaster Tools instead.

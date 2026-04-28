# Admin groups runbook

This page covers the Django **admin panel** groups (`/admin/`). It is unrelated to
the API-layer permission system documented in
[Permissions & Roles](../architecture/permissions.md), which governs `Organization`
owners, staff, and members.

The two systems are independent:

- API permissions are enforced by `RootPermission` subclasses on Django Ninja
  endpoints and live in `OrganizationStaff.permissions` JSON.
- Admin permissions are Django's built-in `auth.Group` + `auth.Permission` system
  and only affect what an authenticated `is_staff` user sees in `/admin/`.

## The `Sales` group

Sales reps need broad admin access to onboard organizations, manage events,
issue invitations, and create discount codes — but not to delete data, modify
users, or change platform configuration.

### Policy at a glance

| Tier | Permissions | Models |
|---|---|---|
| **Full** | view + add + change | `Organization`, `Event`, `EventSeries`, `RecurrenceRule`, `Venue`, `TicketTier`, `MembershipTier`, `EventInvitation`, `PendingEventInvitation`, `EventToken`, `OrganizationToken`, `DiscountCode`, `Tag`, `TagAssignment` |
| **Manage** | view + change | `OrganizationMember`, `OrganizationMembershipRequest`, `EventInvitationRequest`, `EventRSVP`, `EventWaitList`, `Ticket`, `PotluckItem` |
| **View-only** | view | All financial records (`Payment`, `*Invoice`, `*CreditNote`, referrals, billing profiles), `RevelUser`, `Announcement`, compliance models (`Blacklist`, questionnaires), system config (`SiteSettings`, `Legal`, `EmailLog`), notifications, geo, telegram |
| **No access** | none | Security/audit (`GlobalBan`, `ImpersonationLog`, `UserDataExport`, file audit), dietary PII, user privacy preferences |

**No `delete_*` permission is granted on any model.** Deletion of financial,
audit, or user data must go through superuser review.

### Source of truth

The exact tier lists live in
[`src/common/management/commands/sync_sales_group.py`](https://github.com/letsrevel/revel-backend/blob/main/src/common/management/commands/sync_sales_group.py).
Edit that file to change the policy — there is no DB-only configuration.

## Sync command

Run on every deploy to reconcile the group's permissions with the policy in
code:

```bash
python manage.py sync_sales_group
```

The command is idempotent. It uses `Group.objects.get_or_create` and
`group.permissions.set(...)`, so it cleanly picks up adds and removes when the
tier lists change. Missing models or permissions are logged as warnings and do
not abort the run.

!!! tip "Where to invoke it"
    Add this to the deployment script next to `migrate` and `compilemessages`,
    so the group stays in sync as new models are added or moved between tiers.

## Onboarding a sales rep

1. Create the user (or have them register).
2. In the admin user form, set:
    - `is_staff = True` (required for any admin access)
    - `is_superuser = False` (required — superusers bypass the group)
    - Add them to the `Sales` group under **Groups**.
3. Confirm in the sidebar that they only see the apps/models from the policy
   above.

## Admin-side enforcement details

Two pieces of the policy require code, not just permissions:

### Impersonate link hidden for non-superusers

`RevelUserAdmin.get_list_display` removes the `impersonate_link` column for
anyone who isn't a superuser
([`src/accounts/admin/user.py`](https://github.com/letsrevel/revel-backend/blob/main/src/accounts/admin/user.py)).
The underlying impersonation view is also gated by
`accounts.service.impersonation.can_impersonate`, which requires
`is_superuser` — the column hide is purely cosmetic.

### Sensitive `Organization` fields read-only

`OrganizationAdmin.get_readonly_fields` locks these fields for non-superusers
([`src/events/admin/organization.py`](https://github.com/letsrevel/revel-backend/blob/main/src/events/admin/organization.py)):

- `vat_id_validated`, `vat_id_validated_at` — must come from the VAT
  validation flow, not a manual flip
- `platform_fee_percent`, `platform_fee_fixed` — superusers only renegotiate
- `contact_email_verified` — must come from the verification flow

Stripe Connect lives on `RevelUser` (the org owner's account) and the relevant
fields are already in `RevelUserAdmin.readonly_fields` for everyone.

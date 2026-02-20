# PM-0001: Guest User Verification Bypass

## Summary

Guest users created via the unauthenticated checkout/RSVP flow could
escalate to fully authenticated users while retaining `guest=True`. The users
who triggered this were legitimately eligible for the events they participated
in, as they passed all screening gates normally. However, they obtained full JWT
tokens without ever setting a password, effectively receiving a magic-link login
through the email verification flow. The root cause was that the registration and
email verification flows did not distinguish between guest and non-guest users.

## Detection

### Context

Two UX tradeoffs provide relevant context to understand the scenario at hand.

#### 1. Alternative email verification flow
At the beginning of the implementation of Revel a decision was made: if a registered user,
with unverified email, attempts to register again with the same email, the backend:

- silently (re-)sends an email verification link
- responds with a 400

This decision was made to strike a trade-off between security by obscurity and usability:
accounts with unverified emails are eventually deleted
(after a few warning emails over the course of several weeks), and,
except when registering, users do not have a way to navigate to a
"verify my email again" page without logging in first.

#### 2. Guest user handling
It was deemed important to give event organizers the ability to choose whether potential attendees to their events
must create an account on Revel in order to purchase tickets or RSVP

Handling guest checkout is a non-trivial feat. Therefore, in order not to re-architect the platform to enforce
strict separation between `identity` and `users`, it was decided to go with the tradeoff of users marked
as `guest` with an unusable password. This way, for the checkout flows, the same business logic
and models could be used to perform the eligibility checks.
Going through a password reset flow activates the users, verifies their email and removes the `guest` flag.


### Discovery

The issue was discovered during a routine admin panel inspection. Several users
displayed an inconsistent combination of flags:

- `guest = True`
- `email_verified = True`
- Active participation in an event that required a mandatory screening
  questionnaire, which only regular (non-guest) users can submit

Further inspection revealed these users also had pronouns and a preferred name
set, which is only possible via the `PUT /account/me` authenticated endpoint.
Log timestamps compatible with `date_joined` and `last_login` revealed that users were
successfully calling `update_profile`, meaning they held valid JWT tokens.

### Investigating bottom-up

The only way a user obtains a JWT is through `get_token_pair_for_user()` in
`accounts/service/auth.py`. This function is called from exactly two places:

1. **Login endpoints** — `AuthController.obtain_token` (`POST /auth/token/pair`)
   and `AuthController.obtain_token_with_otp` (`POST /auth/token/pair/otp`)
2. **Email verification endpoint** — `AccountController.verify_email`
   (`POST /account/verify-email`)

Guest users have an unusable password (`make_password(None)`), so they cannot
authenticate via the standard login endpoint. That left email verification as
the only viable path.

## Timeline

1. An anonymous user navigates to a public event checkout page.
2. The checkout flow calls `get_or_create_guest_user()`, which creates a
   `RevelUser` with `guest=True`, `email_verified=False`, and an unusable
   password.
3. The backend returns a `400` with an eligibility error: the event requires a
   mandatory screening questionnaire, which only registered users can complete.
4. The frontend, based on the error response, suggests the user to create an
   account and provides a link to `/register`.
5. The user attempts to register with the same email address they used for the
   guest checkout.
6. The frontend displays an error: _"A user with this email already exists"_
   (referring to the guest user created in step 2).
7. **The vulnerability:** `register_user()` in `accounts/service/account.py`
   finds the existing user, sees `email_verified=False`, and re-sends a
   verification email. It does **not** check whether the user is a guest.
8. The guest user receives the verification email and clicks the link.
9. `verify_email()` in `accounts/service/account.py` sets
   `email_verified=True` and `is_active=True`. The `guest` flag is **never
   touched**.
10. The `AccountController.verify_email` endpoint calls
    `get_token_pair_for_user(user)` and returns a full JWT token pair.
11. The frontend logs the user in with the JWT. The user is now fully
    authenticated but still `guest=True`.
12. The user updates their profile (pronouns, preferred name) and submits the
    screening questionnaire: all through legitimate flows, but as a user who
    should never have held a JWT in the first place.

## Root Cause

Two missing guards in the accounts service layer:

### Primary: `register_user()` did not distinguish guest users

When a registration request arrived for an email that already belonged to a
guest user, `register_user()` treated it identically to an unverified regular
user: it re-sent the verification email. This gave guest users access to the
email verification flow, which was never intended for them.

```python
# BEFORE (vulnerable)
if existing_user := RevelUser.objects.filter(username=payload.email).first():
    if not existing_user.email_verified:
        send_verification_email_for_user(existing_user)
    raise HttpError(400, ...)
```

### Secondary: `verify_email()` had no guest guard

Even if a verification token were somehow obtained for a guest user, the
`verify_email()` function would happily verify them and mark them as active.
There was no defense-in-depth check.

### Tertiary: `google_login()` did not clear the guest flag

The Google SSO flow (`google_login()` in `accounts/service/auth.py`) also
lacked a `guest=False` in its `update_or_create` defaults. While the SSO
endpoint is not enabled in the frontend, the API endpoint is live. A guest user
logging in via Google SSO would receive full JWT tokens while keeping
`guest=True`.

## Impact

### Severity: Medium

- **Unintended authentication path**: Guest users obtained full JWT tokens
  without ever setting a password. Functionally equivalent to a magic-link
  login via the email verification flow.
- **Inconsistent state**: Multiple users were found in production with the
  impossible combination `guest=True` AND `email_verified=True`, with
  `last_login` timestamps and profile data (pronouns, preferred name) set.
- **No actual access violation**: The affected users were legitimately eligible
  for the events they participated in — they passed all screening gates
  normally. The issue is that they should not have been able to authenticate
  at all as guest users.

### Not affected

- **Event eligibility**: All affected users went through the questionnaire
  screening legitimately after obtaining tokens.
- **Periodic verification tasks**: `send_early_verification_reminders` and
  `send_final_verification_warnings` correctly filter `guest=False` in their
  querysets.
- **Password login**: Guest users have unusable passwords and cannot
  authenticate via `POST /auth/token/pair`.

## Resolution

Fixed in PR #272 (`fix/guest-user-verification-bypass`), addressing all four
vectors:

### 1. `register_user()` — Guest conversion

When a registration request targets an existing guest email, the function now
**converts** the guest to a full user rather than rejecting with a 400:

- Sets the password from the registration payload
- Updates `first_name` and `last_name`
- Clears `guest=False`
- Resets `email_verified=False` (forces re-verification)
- Sends a verification email

This path is wrapped in `@transaction.atomic` with `select_for_update()` to
prevent race conditions on concurrent registrations for the same guest email.

### 2. `verify_email()` — Defense-in-depth guard

Added a guard that blocks guest users from verifying through this flow. The
guard fires **before** `blacklist_token()`, so the token is never consumed and
repeated attempts are consistently rejected.

```python
if user.guest:
    raise HttpError(400, "Invalid verification token.")
```

### 3. `resend_verification_email()` — Silent rejection

Added a guard that silently rejects guest users, consistent with the existing
enumeration-safe pattern (the function always returns success to prevent email
enumeration).

### 4. `google_login()` — Guest flag clearing

- `ALWAYS_UPDATE` path: Added `"guest": False` to the `defaults` dict, which
  is applied on every login via `update_or_create`.
- `get_or_create` path: Added explicit guest flag clearing when retrieving an
  existing guest user, with `select_for_update()` for concurrency safety.

Both paths ensure a `GoogleSSOUser` record is created for converted guests.

### Test coverage

12 new tests covering:

- Guest-to-full-user conversion via registration
- Conversion of anomalous verified guests (forces re-verification)
- Verification blocking for guest users (single and repeated attempts)
- End-to-end lifecycle: guest → register → verify
- Resend verification silent rejection for guests
- Google SSO guest flag clearing (both `ALWAYS_UPDATE` on and off)
- Non-guest regression: existing duplicate registration flow unchanged
- Password reset conversion for guest users

## Lessons Learned

### 1. Guest users are a distinct security domain

Guest users were introduced as a convenience for unauthenticated checkout, but
the account service layer treated them as regular unverified users. Every flow
that touches user authentication or verification must explicitly account for
the guest state. **The `guest` flag is a security boundary, not just a UX
hint.**

### 2. Impossible states should be enforced, not assumed

The combination `guest=True` AND `email_verified=True` was considered
"impossible" by design but was never enforced with a service-layer guard. If a
state is truly impossible, enforce it — otherwise it will eventually occur.

### 3. Defense-in-depth matters for authentication flows

The primary fix (in `register_user`) is sufficient to close the issue, but
adding guards in `verify_email` and `resend_verification_email` ensures that
even if a future code change introduces a new path to these functions, the
guest boundary holds.

### 4. Admin panel anomalies are a signal

The bug was caught because an admin noticed flags that shouldn't coexist.
Routine admin inspection is a valuable detection mechanism. Consider adding
automated monitoring for "impossible" flag combinations (e.g., a periodic
query for `guest=True, email_verified=True`).

### 5. Unused API endpoints are still attack surface

The Google SSO endpoint is not wired in the frontend, but it is live and
reachable. Any endpoint that issues JWT tokens must be hardened regardless of
whether the frontend uses it.

## References

- **Issue**: [#271 — Security: guest users can obtain full auth tokens via email verification flow](https://github.com/letsrevel/revel-backend/issues/271)
- **Fix PR**: [#272 — fix: prevent guest users from obtaining JWT via email verification flow](https://github.com/letsrevel/revel-backend/pull/272)
- **Affected files**: `accounts/service/account.py`, `accounts/service/auth.py`
- **Test files**: `accounts/tests/test_account_service.py`, `accounts/tests/test_auth_service.py`

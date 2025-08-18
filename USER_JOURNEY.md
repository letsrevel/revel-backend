# Revel User Journey

This document outlines the primary user flows and interactions within the Revel platform, from account creation to event management. It is intended for developers and contributors to understand the application's architecture and business logic. Each flow links to the relevant source code controllers and services, or to an open issue for features that are not yet implemented.

## Account Flows

These flows cover the entire lifecycle of a user account, from registration to deletion. All related logic is primarily handled by the `accounts` app.

### 1. New User Registration & Email Verification

A new user signs up with their email and password. The system creates an inactive account and sends a verification email.

1.  **Endpoint:** `POST /api/account/register`
2.  **Controller:** `accounts.controllers.account.AccountController.register`
3.  **Logic:** The controller calls the `accounts.service.account.register_user` service function.
    *   It first checks if a user with that email already exists. If an unverified user exists, it resends the verification email. If a verified user exists, it returns an error.
    *   A new `RevelUser` is created.
    *   A unique, short-lived JWT is generated for email verification.
    *   The `accounts.tasks.send_verification_email` Celery task is dispatched to send the email containing the verification link.
    ```python
    # src/accounts/service/account.py

    def register_user(payload: schema.RegisterUserSchema) -> tuple[RevelUser, str]:
        # ... user existence check ...
        new_user = RevelUser.objects.create_user(...)
        # ... token creation ...
        tasks.send_verification_email.delay(user.email, token)
        return user, token
    ```
4.  **Verification:** The user clicks the link, which calls the verification endpoint.
    *   **Endpoint:** `POST /api/account/verify`
    *   **Controller:** `accounts.controllers.account.AccountController.verify_email`
    *   **Logic:** The `accounts.service.account.verify_email` service validates the token, marks the user's `email_verified` field as `True`, and returns a new access/refresh token pair for immediate login.

### 2. User Authentication

Users can log in using standard credentials or Google SSO. The system supports Time-based One-Time Passwords (TOTP) for enhanced security.

1.  **Standard Login:**
    *   **Endpoint:** `POST /api/auth/token/pair`
    *   **Controller:** `accounts.controllers.auth.AuthController.obtain_token`
    *   **Logic:**
        *   If the user does **not** have 2FA enabled, it returns a standard JWT access/refresh token pair.
        *   If the user **has 2FA enabled**, it returns a temporary, single-purpose token. The user must then call the OTP endpoint with this temporary token and their TOTP code.
2.  **2FA (TOTP) Login:**
    *   **Endpoint:** `POST /api/auth/token/pair/otp`
    *   **Controller:** `accounts.controllers.auth.AuthController.obtain_token_with_otp`
    *   **Logic:** The `accounts.service.auth.verify_otp_jwt` service validates both the temporary token and the user's provided TOTP code. If successful, it returns a standard JWT access/refresh token pair.
3.  **Google SSO:**
    *   **Endpoint:** `POST /api/auth/google/login`
    *   **Controller:** `accounts.controllers.auth.AuthController.google_login`
    *   **Logic:** The `accounts.service.auth.google_login` service takes the Google ID token, verifies it with Google, and then gets or creates a `RevelUser`. It returns a standard JWT access/refresh token pair.

### 3. Profile & Password Management

Authenticated users can manage their profile information and password.

1.  **View/Update Profile:**
    *   **Endpoint:** `GET /api/account/me` and `PUT /api/account/me`
    *   **Controller:** `accounts.controllers.account.AccountController` (`me` and `update_profile` methods)
2.  **Password Reset:**
    *   A user requests a password reset by providing their email.
    *   **Endpoint:** `POST /api/account/password/reset-request`
    *   **Logic:** The `accounts.service.account.request_password_reset` service generates a reset token and dispatches the `accounts.tasks.send_password_reset_link` task. The endpoint always returns a generic success message to prevent user enumeration.
    *   The user receives an email, clicks the link, and submits a new password.
    *   **Endpoint:** `POST /api/account/password/reset`
    *   **Logic:** `accounts.service.account.reset_password` validates the token and updates the user's password.

### 4. Account Deletion

Users have the right to delete their accounts.

1.  **Deletion Request:**
    *   **Endpoint:** `POST /api/account/delete-request`
    *   **Logic:** `accounts.service.account.request_account_deletion` generates a deletion token and sends a confirmation email.
2.  **Deletion Confirmation:**
    *   **Endpoint:** `POST /api/account/delete-confirm`
    *   **Logic:** `accounts.service.account.confirm_account_deletion` validates the token and deletes the user.
    *   **Current Limitation:** The process currently fails if the user has associated `Payment` records due to a `ProtectedError`. This is a known bug that will be addressed by anonymizing financial records instead of hard-deleting the user.
    *   **Reference:** [Issue #9: User deletion fails with `ProtectedError`](https://github.com/letsrevel/revel-backend/issues/9)

## Core Flows

These are complex, significant flows that represent the core business logic of the Revel platform.

### 1. Event Eligibility Check

This is the most critical flow in Revel, determining if a user can participate in an event. It's designed as a pipeline of "gates," where a user must pass through each one to be granted access. This logic is centralized in the `EligibilityService`.

*   **Service:** `events.service.event_manager.EligibilityService`
*   **Trigger:** This service is called by multiple controller methods, including `rsvp_event`, `ticket_checkout`, and `get_my_event_status`.

The eligibility gates are checked in the following order:

1.  **Privileged Access Gate:** Immediately grants access to organization owners and staff.
    ```python
    # src/events/service/event_manager.py
    class PrivilegedAccessGate(BaseEligibilityGate):
        def check(self) -> EventUserEligibility | None:
            if self.event.organization.owner_id == self.user.id or self.user.id in self.handler.staff_ids:
                return EventUserEligibility(allowed=True, tier="staff", event_id=self.event.pk)
            return None
    ```2.  **Event Status Gate:** Checks if the event is open and has not ended.
3.  **RSVP Deadline Gate:** For non-ticketed events, checks if the `rsvp_before` deadline has passed. Can be waived by an invitation.
4.  **Invitation Gate:** For private events, checks if the user has a valid `EventInvitation`.
5.  **Membership Gate:** For members-only events, checks if the user is a member of the organization. Can be waived by an invitation.
6.  **Questionnaire Gate:** Checks if all required admission questionnaires have been submitted and approved. This is a multi-step check:
    *   Verifies that a submission exists for each required questionnaire.
    *   Checks if any submissions are pending manual review.
    *   Checks if any submissions were rejected and if the user is allowed to retake them.
7.  **Availability Gate:** Checks if `max_attendees` has been reached. Can be waived by an invitation.
8.  **Ticket Sales Gate:** For ticketed events, checks if at least one ticket tier is within its `sales_start_at` and `sales_end_at` window.

If the user fails any gate, the service returns an `EventUserEligibility` object detailing why they were denied access and what the potential `next_step` is (e.g., `COMPLETE_QUESTIONNAIRE`, `JOIN_WAITLIST`).

### 2. Questionnaire Submission & Evaluation

This flow manages how users are screened for events via questionnaires.

*   **Service:** `questionnaires.service.QuestionnaireService`

1.  **Submission:**
    *   **Endpoint:** `POST /api/events/{event_id}/questionnaire/{questionnaire_id}/submit`
    *   **Controller:** `events.controllers.events.EventController.submit_questionnaire`
    *   **Logic:** The `QuestionnaireService.submit` method is called. It validates that all mandatory questions are answered and creates `QuestionnaireSubmission`, `MultipleChoiceAnswer`, and `FreeTextAnswer` records.
2.  **Evaluation Trigger:**
    *   After a successful submission, the controller dispatches a Celery task: `questionnaires.tasks.evaluate_questionnaire_submission`.
3.  **Automatic Evaluation:**
    *   **Task:** The Celery task uses the `questionnaires.evaluator.SubmissionEvaluator` service.
    *   **Logic:** The `SubmissionEvaluator` calculates a score based on multiple-choice answers and, for free-text questions, calls a configurable LLM backend (e.g., `VulnerableChatGPTEvaluator`) to get a pass/fail result. It creates a `QuestionnaireEvaluation` record with the results.
    *   If the questionnaire's `evaluation_mode` is `AUTOMATIC`, the evaluation status is set directly to `APPROVED` or `REJECTED`. If it's `HYBRID` or `MANUAL`, it's set to `PENDING_REVIEW`.

## Attendee Flows

These are common flows for users looking to attend events.

1.  **Browsing Events & Organizations:**
    *   **Endpoints:** `GET /api/events/`, `GET /api/organizations/`
    *   **Logic:** Visibility is handled by the `for_user` manager method on the `Event` and `Organization` models, which constructs an efficient query to only show items the user is allowed to see.
2.  **RSVPing to an Event:**
    *   **Endpoint:** `POST /api/events/{event_id}/rsvp/{answer}`
    *   **Controller:** `events.controllers.events.EventController.rsvp_event`
    *   **Logic:** This flow uses the `EventManager` which first runs the full **Event Eligibility Check**. If successful, it creates or updates the user's `EventRSVP` record. This is only possible for events where `requires_ticket` is `False`.
3.  **Getting a Ticket:**
    *   **Endpoints:**
        *   `POST /api/events/{event_id}/tickets/{tier_id}/checkout` (for fixed-price tickets)
        *   `POST /api/events/{event_id}/tickets/{tier_id}/checkout/pwyc` (for "Pay What You Can" tickets)
    *   **Controller:** `events.controllers.events.EventController` (`ticket_checkout` and `ticket_pwyc_checkout` methods)
    *   **Logic:** This flow also uses the `EventManager` and its eligibility checks.
        *   For **free** or **offline/at-the-door** payment tiers, a `Ticket` is created directly.
        *   For **online** payment tiers, the `events.service.stripe_service` is called to create a Stripe Checkout Session, and a `Payment` record is created in a `PENDING` state. The user is redirected to Stripe to complete the purchase.
4.  **Managing Potluck Items:**
    *   **Endpoints:** `GET`, `POST` on `/api/events/{event_id}/potluck/` and `POST` on `/api/events/{event_id}/potluck/{item_id}/claim`
    *   **Controller:** `events.controllers.potluck.PotluckController`
    *   **Logic:** Attendees of an event where `potluck_open` is true can list, create, and claim items.
    *   **Future Work:** Unassigning items when a user cancels their attendance is a planned feature. ([Issue #28](https://github.com/letsrevel/revel-backend/issues/28))

## Organizer Flows

These are common flows for Organization Owners and Staff. Access is controlled by a granular permission system.

1.  **Managing Organization Details:**
    *   **Endpoint:** `PUT /api/organization-admin/{slug}`
    *   **Controller:** `events.controllers.organization_admin.OrganizationAdminController.update_organization`
    *   **Permissions:** `edit_organization`
2.  **Managing Members and Staff:**
    *   **Endpoints:** `GET /api/organization-admin/{slug}/members`, `DELETE /api/organization-admin/{slug}/members/{user_id}`, `POST /api/organization-admin/{slug}/staff/{user_id}`
    *   **Controller:** `events.controllers.organization_admin.OrganizationAdminController`
    *   **Logic:** These endpoints allow owners (and staff with `manage_members` permission) to view, add, and remove members and staff. The permission system is defined in `events.models.organization.PermissionsSchema` and checked by `events.controllers.permissions.OrganizationPermission`.
3.  **Creating Events:**
    *   **Endpoint:** `POST /api/organization-admin/{slug}/create-event`
    *   **Controller:** `events.controllers.organization_admin.OrganizationAdminController.create_event`
    *   **Permissions:** `create_event`
4.  **Managing Event Invitations:**
    *   **Endpoint:** `POST /api/event-admin/{event_id}/invitations`
    *   **Controller:** `events.controllers.event_admin.EventAdminController.create_invitations`
    *   **Logic:** The `events.service.invitation_service.create_direct_invitations` handles the logic of creating `EventInvitation` objects for existing users and `PendingEventInvitation` objects for users not yet on the platform.
    *   **Future Work:** Sending email notifications for these invitations is a planned feature. ([Issue #23](https://github.com/letsrevel/revel-backend/issues/23))
5.  **Building Questionnaires:**
    *   **Endpoints:** `POST /api/questionnaires/{org_questionnaire_id}/sections`, `POST /api/questionnaires/{org_questionnaire_id}/multiple-choice-questions`, etc.
    *   **Controller:** `events.controllers.questionnaire.QuestionnaireController`
    *   **Logic:** Organizers can build complex questionnaires with sections and different question types. This is handled by the `QuestionnaireService`.
    *   **Known Bug:** Updating sections or questions is currently destructive, which can lead to data integrity issues with existing answers. ([Issue #1](https://github.com/letsrevel/revel-backend/issues/1))
6.  **Reviewing Questionnaire Submissions:**
    *   **Endpoints:** `GET /api/questionnaires/{org_questionnaire_id}/submissions`, `POST /api/questionnaires/{org_questionnaire_id}/submissions/{submission_id}/evaluate`
    *   **Controller:** `events.controllers.questionnaire.QuestionnaireController`
    *   **Logic:** Organizers with `evaluate_questionnaire` permission can view submissions and manually approve or reject them, which updates the `QuestionnaireEvaluation` record.
7.  **Managing Event Check-in:**
    *   **Endpoint:** `POST /api/event-admin/{event_id}/check-in`
    *   **Controller:** `events.controllers.event_admin.EventAdminController.check_in_ticket`
    *   **Logic:** Staff with `check_in_attendees` permission can check in users by their ticket ID, changing the ticket status to `CHECKED_IN` if the check-in window is open.
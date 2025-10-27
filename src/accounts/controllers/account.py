"""This module contains the controllers for the authentication app."""

import typing as t

from ninja_extra import ControllerBase, api_controller, route, status
from ninja_jwt.authentication import JWTAuth

from accounts import schema, tasks
from accounts.models import RevelUser
from accounts.schema import (
    ProfileUpdateSchema,
    RevelUserSchema,
)
from accounts.service import account as account_service
from accounts.service.auth import get_token_pair_for_user
from common.schema import ResponseMessage
from common.throttling import AuthThrottle, UserDataExportThrottle


@api_controller("/account", tags=["Account"], throttle=AuthThrottle())
class AccountController(ControllerBase):
    def user(self) -> RevelUser:
        """Get the user for this request."""
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    @route.post(
        "/export-data",
        response={200: ResponseMessage},
        url_name="export-data",
        auth=JWTAuth(),
        throttle=UserDataExportThrottle(),
    )
    def export_data(self) -> ResponseMessage:
        """Request a GDPR-compliant export of all personal data.

        Initiates an asynchronous export of all user data including profile, events, tickets,
        and submissions. The export will be emailed to the user when ready. Rate-limited to
        prevent abuse.
        """
        tasks.generate_user_data_export.delay(str(self.user().pk))
        return ResponseMessage(message="Your data export has been initiated.")

    @route.get(
        "/me",
        response=RevelUserSchema,
        url_name="me",
        auth=JWTAuth(),
    )
    def me(self) -> RevelUser:
        """Retrieve the authenticated user's profile information.

        Returns complete user profile including email, name, location preferences, and 2FA status.
        Use this to display user info in the UI or verify authentication status.
        """
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    @route.put(
        "/me",
        response=RevelUserSchema,
        url_name="update-profile",
        auth=JWTAuth(),
    )
    def update_profile(self, payload: ProfileUpdateSchema) -> RevelUser:
        """Update the authenticated user's profile information.

        Allows updating name, location preferences, and other profile fields. Only provided
        fields are updated. Returns the updated user profile.
        """
        user = self.user()
        for key, value in payload.dict().items():
            setattr(user, key, value)
        user.save(update_fields=list(payload.dict().keys()))
        return user

    @route.post(
        "/register",
        tags=["Account"],
        response={201: RevelUserSchema},
        url_name="register-account",
    )
    def register(self, payload: schema.RegisterUserSchema) -> tuple[int, RevelUser]:
        """Create a new user account with email and password.

        Creates a new account and sends a verification email. The account is created but not
        fully active until email is verified via POST /account/verify. If an unverified account
        with the same email exists, resends the verification email. Returns 400 if a verified
        account already exists.
        """
        user, _ = account_service.register_user(payload)
        return status.HTTP_201_CREATED, user

    @route.post(
        "/verify",
        tags=["Account"],
        response=schema.VerifyEmailResponseSchema,
        url_name="verify-email",
    )
    def verify_email(self, payload: schema.VerifyEmailSchema) -> schema.VerifyEmailResponseSchema:
        """Verify email address using the token from the verification email.

        Call this with the token received via email after registration. On success, activates
        the account and returns the verified user profile along with JWT tokens for immediate login.
        The verification token is single-use and expires after a set period.
        """
        user = account_service.verify_email(payload.token)
        token = get_token_pair_for_user(user)
        return schema.VerifyEmailResponseSchema(user=RevelUserSchema.from_orm(user), token=token)

    @route.post(
        "/verify-resend",
        tags=["Account"],
        response={200: ResponseMessage, 400: ResponseMessage},
        url_name="resend-verification-email",
        auth=JWTAuth(),
    )
    def resend_verification_email(self) -> tuple[int, ResponseMessage]:
        """Resend the email verification link to the authenticated user.

        Use this if the original verification email was lost or expired. Returns 400 if the
        email is already verified. Requires authentication with the unverified account's JWT.
        """
        user = self.user()
        if user.email_verified:
            return status.HTTP_400_BAD_REQUEST, ResponseMessage(message="Email already verified.")
        account_service.send_verification_email_for_user(user)
        return status.HTTP_200_OK, ResponseMessage(message="Verification email sent.")

    @route.post(
        "/delete-request",
        tags=["Account"],
        response=ResponseMessage,
        url_name="delete-account-request",
        auth=JWTAuth(),
    )
    def delete_account_request(self) -> ResponseMessage:
        """Initiate GDPR-compliant account deletion by sending confirmation email.

        Sends an email with a deletion confirmation link. The account is not deleted until
        the user confirms via POST /account/delete-confirm with the token from the email.
        This two-step process prevents accidental deletions.
        """
        account_service.request_account_deletion(self.user())
        return ResponseMessage(message="An email has been sent.")

    @route.post(
        "/delete-confirm",
        tags=["Account"],
        response=ResponseMessage,
        url_name="delete-account-confirm",
    )
    def delete_account_confirm(self, payload: schema.DeleteAccountConfirmSchema) -> ResponseMessage:
        """Permanently delete the account using the confirmation token from email.

        Call this with the token received via email after POST /account/delete-request.
        This action is irreversible and deletes all user data. The deletion is processed
        asynchronously in the background. The deletion token is single-use and expires
        after a set period.
        """
        account_service.confirm_account_deletion(payload.token)
        return ResponseMessage(message="Your account deletion has been initiated and will be processed shortly.")

    @route.post(
        "/password/reset-request",
        tags=["Account"],
        response=ResponseMessage,
        url_name="reset-password-request",
    )
    def reset_password_request(self, payload: schema.PasswordResetRequestSchema) -> ResponseMessage:
        """Request a password reset by email.

        Sends a password reset link to the provided email if an account exists. Always returns
        a success message to prevent user enumeration attacks. Google SSO users cannot use this
        endpoint. After receiving the email, use POST /account/password/reset with the token.
        """
        account_service.request_password_reset(payload.email)
        return ResponseMessage(message="A password reset link will be sent.")

    @route.post(
        "/password/reset",
        tags=["Account"],
        response=ResponseMessage,
        url_name="reset-password",
    )
    def reset_password(self, payload: schema.PasswordResetSchema) -> ResponseMessage:
        """Reset password using the token from the password reset email.

        Call this with the token received via email after POST /account/password/reset-request.
        The new password must meet security requirements. The reset token is single-use and
        expires after a set period. After reset, the user must login again with the new password.
        """
        account_service.reset_password(payload.token, payload.password1)
        return ResponseMessage(message="Password reset successfully.")

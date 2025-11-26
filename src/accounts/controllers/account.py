"""This module contains the controllers for the authentication app."""

import typing as t

from django.utils.translation import gettext_lazy as _
from ninja_extra import ControllerBase, api_controller, route, status

from accounts import schema, tasks
from accounts.models import RevelUser
from accounts.schema import (
    ProfileUpdateSchema,
    RevelUserSchema,
)
from accounts.service import account as account_service
from accounts.service.auth import get_token_pair_for_user
from common.authentication import I18nJWTAuth
from common.schema import EmailSchema, ResponseMessage
from common.throttling import AuthThrottle, UserDataExportThrottle, UserRegistrationThrottle


@api_controller("/account", tags=["Account"], throttle=AuthThrottle())
class AccountController(ControllerBase):
    def user(self) -> RevelUser:
        """Get the user for this request."""
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    @route.post(
        "/export-data",
        response={200: ResponseMessage},
        url_name="export-data",
        auth=I18nJWTAuth(),
        throttle=UserDataExportThrottle(),
    )
    def export_data(self) -> ResponseMessage:
        """Request a GDPR-compliant export of all personal data.

        Initiates an asynchronous export of all user data including profile, events, tickets,
        and submissions. The export will be emailed to the user when ready. Rate-limited to
        prevent abuse.
        """
        tasks.generate_user_data_export.delay(str(self.user().pk))
        return ResponseMessage(message=str(_("Your data export has been initiated.")))

    @route.get(
        "/me",
        response=RevelUserSchema,
        url_name="me",
        auth=I18nJWTAuth(),
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
        auth=I18nJWTAuth(),
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
        throttle=UserRegistrationThrottle(),
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
        response=ResponseMessage,
        url_name="resend-verification-email",
        throttle=UserRegistrationThrottle(),
    )
    def resend_verification_email(self, payload: EmailSchema) -> ResponseMessage:
        """Resend the email verification link for a given email address.

        Use this if the original verification email was lost or expired. Always returns a
        success message to prevent user enumeration attacks. The email is only sent if the
        account exists and is not yet verified.
        """
        account_service.resend_verification_email(payload.email)
        return ResponseMessage(message=str(_("Verification email sent.")))

    @route.post(
        "/delete-request",
        tags=["Account"],
        response=ResponseMessage,
        url_name="delete-account-request",
        auth=I18nJWTAuth(),
    )
    def delete_account_request(self) -> ResponseMessage:
        """Initiate GDPR-compliant account deletion by sending confirmation email.

        Sends an email with a deletion confirmation link. The account is not deleted until
        the user confirms via POST /account/delete-confirm with the token from the email.
        This two-step process prevents accidental deletions.
        """
        account_service.request_account_deletion(self.user())
        return ResponseMessage(message=str(_("An email has been sent.")))

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
        return ResponseMessage(
            message=str(_("Your account deletion has been initiated and will be processed shortly."))
        )

    @route.post(
        "/password/reset-request",
        tags=["Account"],
        response=ResponseMessage,
        url_name="reset-password-request",
        throttle=UserRegistrationThrottle(),
    )
    def reset_password_request(self, payload: EmailSchema) -> ResponseMessage:
        """Request a password reset by email.

        Sends a password reset link to the provided email if an account exists. Always returns
        a success message to prevent user enumeration attacks. Google SSO users cannot use this
        endpoint. After receiving the email, use POST /account/password/reset with the token.
        """
        account_service.request_password_reset(payload.email)
        return ResponseMessage(message=str(_("A password reset link will be sent.")))

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
        return ResponseMessage(message=str(_("Password reset successfully.")))

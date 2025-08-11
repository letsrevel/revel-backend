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
        """Request an export of all personal data."""
        tasks.generate_user_data_export.delay(str(self.user().pk))
        return ResponseMessage(message="Your data export has been initiated.")

    @route.get(
        "/me",
        response=RevelUserSchema,
        url_name="me",
        auth=JWTAuth(),
    )
    def me(self) -> RevelUser:
        """Get the user for this request."""
        return t.cast(RevelUser, self.context.request.user)  # type: ignore[union-attr]

    @route.put(
        "/me",
        response=RevelUserSchema,
        url_name="update-profile",
        auth=JWTAuth(),
    )
    def update_profile(self, payload: ProfileUpdateSchema) -> RevelUser:
        """Update user profile information."""
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
        """Register a new user."""
        user, _ = account_service.register_user(payload)
        return status.HTTP_201_CREATED, user

    @route.post(
        "/verify",
        tags=["Account"],
        response=schema.VerifyEmailResponseSchema,
        url_name="verify-email",
    )
    def verify_email(self, payload: schema.VerifyEmailSchema) -> schema.VerifyEmailResponseSchema:
        """Verify a user's email."""
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
        """Resend the verification email."""
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
        """Request account deletion by sending a confirmation email with token."""
        account_service.request_account_deletion(self.user())
        return ResponseMessage(message="An email has been sent.")

    @route.post(
        "/delete-confirm",
        tags=["Account"],
        response=ResponseMessage,
        url_name="delete-account-confirm",
    )
    def delete_account_confirm(self, payload: schema.DeleteAccountConfirmSchema) -> ResponseMessage:
        """Delete the account using the token from the email."""
        account_service.confirm_account_deletion(payload.token)
        return ResponseMessage(message="Account deleted successfully.")

    @route.post(
        "/password/reset-request",
        tags=["Account"],
        response=ResponseMessage,
        url_name="reset-password-request",
    )
    def reset_password_request(self, payload: schema.PasswordResetRequestSchema) -> ResponseMessage:
        """Request a password reset."""
        account_service.request_password_reset(payload.email)
        return ResponseMessage(message="A password reset link will be sent.")

    @route.post(
        "/password/reset",
        tags=["Account"],
        response=ResponseMessage,
        url_name="reset-password",
    )
    def reset_password(self, payload: schema.PasswordResetSchema) -> ResponseMessage:
        """Reset a user's password."""
        account_service.reset_password(payload.token, payload.password1)
        return ResponseMessage(message="Password reset successfully.")

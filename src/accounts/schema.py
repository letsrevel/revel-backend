"""Schema for accounts module."""

import datetime
import typing as t
from uuid import uuid4

from django.conf import settings
from ninja import ModelSchema, Schema
from ninja_jwt.schema import TokenObtainPairOutputSchema
from pydantic import UUID4, EmailStr, Field, field_serializer, model_validator

from accounts.password_validation import validate_password
from common.schema import StrippedString

from .models import RevelUser


class RevelUserSchema(ModelSchema):
    id: UUID4
    email: str
    email_verified: bool
    preferred_name: str
    pronouns: str
    is_active: bool
    first_name: str
    last_name: str
    totp_active: bool

    class Meta:
        model = RevelUser
        fields = ["email", "email_verified", "is_active", "first_name", "last_name", "totp_active"]


class MinimalRevelUserSchema(ModelSchema):
    class Meta:
        model = RevelUser
        fields = ["preferred_name", "pronouns", "first_name", "last_name"]


class MemberUserSchema(ModelSchema):
    class Meta:
        model = RevelUser
        fields = ["email", "phone_number", "preferred_name", "pronouns", "first_name", "last_name"]


class TOTPProvisioningUriSchema(Schema):
    uri: str = Field(..., description="The provisioning URI for the TOTP app, to be rendered in a QR code.")


class OTPVerifySchema(Schema):
    otp: str = Field(..., description="The one-time password to verify.", max_length=6)


class TempToken(Schema):
    token: str = Field(..., description="The temporary token to be used with OTP.")
    type: t.Literal["otp"] = "otp"


class TempTokenWithTOTP(Schema):
    token: str
    otp: str = Field(..., description="The one-time password to verify.", max_length=6)


class GoogleIDTokenSchema(Schema):
    id_token: str = Field(..., description="The Google ID token to verify.")


class GoogleIDInfo(Schema):
    email: str = ""
    given_name: str = ""
    family_name: str = ""
    sub: str = ""
    picture: str = ""
    locale: str = ""


class WebAuthnRegisterOptionsSchema(Schema):
    options: dict[str, t.Any]


class WebAuthnRegisterResponseSchema(Schema):
    status: str


class WebAuthnLoginOptionsSchema(Schema):
    options: dict[str, t.Any]


class WebAuthnLoginResponseSchema(Schema):
    status: str
    message: str | None = None
    token: str | None = None


class WebAuthnVerifyRegisterSchema(Schema):
    credential: dict[str, t.Any]


class WebAuthnVerifyLoginSchema(Schema):
    credential: dict[str, t.Any]


class PasswordMixin(Schema):
    password1: str = Field(..., description="Password", min_length=8, max_length=150)
    password2: str = Field(..., description="Password confirmation", min_length=8, max_length=150)

    @model_validator(mode="after")
    def password_match(self) -> t.Self:
        """Validate that the passwords match."""
        if self.password1 != self.password2:
            raise ValueError("Passwords do not match")
        return self


class RegisterUserSchema(PasswordMixin):
    email: EmailStr
    first_name: StrippedString = ""
    last_name: StrippedString = ""

    @model_validator(mode="after")
    def validate_password(self) -> t.Self:
        """Validate the password."""
        tmp_user = RevelUser(
            email=self.email, username=self.email, first_name=self.first_name, last_name=self.last_name
        )
        validate_password(self.password1, user=tmp_user)
        return self


class VerifyEmailSchema(Schema):
    token: str


class _BaseEmailJWTPayloadSchema(Schema):
    user_id: UUID4
    email: EmailStr
    exp: datetime.datetime
    jti: str = Field(default_factory=lambda: str(uuid4()))
    aud: str = Field(default_factory=lambda: settings.JWT_AUDIENCE)

    @field_serializer("exp")
    def serialize_exp(self, value: datetime.datetime) -> int:
        return int(value.timestamp())


class VerifyEmailJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    type: t.Literal["email_verification"] = "email_verification"


class PasswordResetJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    type: t.Literal["password_reset"] = "password_reset"


class DeleteAccountJWTPayloadSchema(_BaseEmailJWTPayloadSchema):
    type: t.Literal["delete_account"] = "delete_account"


class DeleteAccountSchema(Schema):
    password: str


class DeleteAccountConfirmSchema(Schema):
    token: str


class PasswordResetRequestSchema(Schema):
    email: EmailStr


class PasswordResetSchema(PasswordMixin):
    token: str


class ChangePasswordSchema(PasswordMixin):
    old_password: str


class ProfileUpdateSchema(Schema):
    """Schema for updating user profile information."""

    preferred_name: str = Field(..., max_length=255, description="User's preferred name")
    pronouns: str = Field(..., max_length=10, description="User's pronouns")
    first_name: str = Field(..., max_length=30, description="User's first name")
    last_name: str = Field(..., max_length=150, description="User's last name")


class VerifyEmailResponseSchema(Schema):
    user: RevelUserSchema
    token: TokenObtainPairOutputSchema

"""Account-action tokens must carry their own ``type`` claim.

The payload schemas default ``type`` so they can be constructed without it, but a decoded token
that omits the claim must not inherit the expected purpose: session tokens (signed with the same
key and audience, carrying ``user_id``/``email``/``exp``/``jti``) would otherwise validate as
email-verification, password-reset, deletion or unsubscribe tokens.
"""

import typing as t

import pytest
from ninja import Schema
from ninja.errors import HttpError
from ninja_extra.exceptions import AuthenticationFailed

from accounts import schema
from accounts.jwt import validate_otp_jwt
from accounts.models import RevelUser
from accounts.service import account as account_service
from accounts.service.auth import get_token_pair_for_user
from events.schema import VerifyOrganizationContactEmailJWTPayloadSchema
from notifications.schema import EmailOptOutJWTPayloadSchema, OneClickUnsubscribePayload

pytestmark = pytest.mark.django_db

ACTION_SCHEMAS: list[type[schema.BaseEmailJWTPayloadSchema]] = [
    schema.VerifyEmailJWTPayloadSchema,
    schema.PasswordResetJWTPayloadSchema,
    schema.DeleteAccountJWTPayloadSchema,
    schema.UnsubscribeJWTPayloadSchema,
]


@pytest.mark.parametrize(
    "schema_class",
    [
        *ACTION_SCHEMAS,
        schema.EmailChangeJWTPayloadSchema,
        VerifyOrganizationContactEmailJWTPayloadSchema,
        OneClickUnsubscribePayload,
        EmailOptOutJWTPayloadSchema,
    ],
)
@pytest.mark.parametrize("which", ["access", "refresh"])
def test_session_token_is_not_an_action_token(
    unverified_user: RevelUser,
    schema_class: type[Schema],
    which: t.Literal["access", "refresh"],
) -> None:
    pair = get_token_pair_for_user(unverified_user)
    with pytest.raises(HttpError) as exc:
        account_service.token_to_payload(getattr(pair, which), schema_class)
    assert exc.value.status_code == 400


def test_session_token_cannot_verify_email(unverified_user: RevelUser) -> None:
    pair = get_token_pair_for_user(unverified_user)
    with pytest.raises(HttpError):
        account_service.verify_email(pair.access)
    unverified_user.refresh_from_db()
    assert unverified_user.email_verified is False


def test_session_token_cannot_reset_password(unverified_user: RevelUser) -> None:
    pair = get_token_pair_for_user(unverified_user)
    with pytest.raises(HttpError):
        account_service.reset_password(pair.refresh, "a-New-strong-password-456!")
    unverified_user.refresh_from_db()
    assert not unverified_user.check_password("a-New-strong-password-456!")


@pytest.mark.parametrize("schema_class", ACTION_SCHEMAS)
def test_genuine_action_token_still_validates(
    unverified_user: RevelUser, schema_class: type[schema.BaseEmailJWTPayloadSchema]
) -> None:
    from django.conf import settings
    from django.utils import timezone

    from accounts.jwt import create_token

    payload = schema_class(
        user_id=unverified_user.id, email=unverified_user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    assert account_service.token_to_payload(token, schema_class).user_id == unverified_user.id


@pytest.mark.parametrize("which", ["access", "refresh"])
def test_session_token_is_not_an_otp_token(unverified_user: RevelUser, which: t.Literal["access", "refresh"]) -> None:
    pair = get_token_pair_for_user(unverified_user)
    with pytest.raises(AuthenticationFailed):
        validate_otp_jwt(getattr(pair, which))

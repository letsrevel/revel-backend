"""Authentication service layer."""

from datetime import datetime, timedelta
from uuid import uuid4

import pyotp
import structlog
from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_google_sso.models import GoogleSSOUser
from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_oauth2_token as _verify_oauth2_token
from ninja.errors import HttpError
from ninja_jwt.schema import TokenObtainPairOutputSchema
from ninja_jwt.tokens import RefreshToken

from accounts import schema
from accounts.jwt import TOTPJWTPayload, check_blacklist, create_token, validate_otp_jwt
from accounts.jwt import blacklist as blacklist_token
from accounts.models import RevelUser

logger = structlog.get_logger(__name__)


def get_temporary_otp_jwt(user: RevelUser) -> str:
    """Get a temporary JWT.

    This will need to be used by a user in combination with their OTP code to obtain a valid JWT.
    """
    logger.info("otp_jwt_generated", user_id=str(user.id), email=user.email)
    payload = TOTPJWTPayload(
        sub=str(user.id),
        aud=settings.JWT_AUDIENCE,
        email=user.email,
        user_id=user.id,
        iat=datetime.now(),
        exp=datetime.now() + timedelta(minutes=5),
        jti=uuid4().hex,
    )
    return create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)


@transaction.atomic
def verify_otp_jwt(token: str, otp: str) -> tuple[RevelUser, bool]:
    """Verify the OTP JWT token."""
    validated_token = validate_otp_jwt(
        token, key=settings.SECRET_KEY, audience=settings.JWT_AUDIENCE, algorithms=[settings.JWT_ALGORITHM]
    )
    check_blacklist(validated_token.jti)
    if validated_token.type != "totp-access":
        logger.warning("otp_verification_invalid_token_type", token_type=validated_token.type)  # type: ignore[unreachable]
        raise HttpError(401, str(_("Invalid token type.")))
    blacklist_token(token)
    user = get_object_or_404(RevelUser, id=validated_token.user_id)
    totp = pyotp.TOTP(user.totp_secret)
    is_valid = totp.verify(otp)
    logging_method, result = (
        (logger.info, "otp_verification_success") if is_valid else (logger.warning, "otp_verification_failure")
    )
    logging_method(result, user_id=str(user.id), email=user.email)
    return user, is_valid


def get_token_pair_for_user(user: RevelUser) -> TokenObtainPairOutputSchema:
    """Get a token pair for the user."""
    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])

    logger.info("token_pair_generated", user_id=str(user.id), email=user.email)
    token = RefreshToken.for_user(user)
    token.payload.update(schema.RevelUserSchema.from_orm(user).model_dump(mode="json"))
    token.payload.update(
        {
            "sub": str(user.id),
            "groups": list(user.groups.values_list("name", flat=True)),
            "is_staff": user.is_staff,
            "is_superuser": user.is_superuser,
        }
    )
    return TokenObtainPairOutputSchema(
        username=user.username,
        access=str(token.access_token),  # type: ignore[attr-defined]
        refresh=str(token),
    )


def google_login(id_token: str) -> TokenObtainPairOutputSchema:
    """Log in or register a user using Google SSO."""
    id_info = verify_oauth2_token(id_token)

    # Extract language from Google locale (e.g., "en-US" -> "en", "de-DE" -> "de")
    language = settings.LANGUAGE_CODE  # Default
    if id_info.locale:
        locale_lang = id_info.locale.split("-")[0]
        # Check if it's a supported language
        supported_languages = [lang[0] for lang in settings.LANGUAGES]
        if locale_lang in supported_languages:
            language = locale_lang

    defaults = {
        "email": id_info.email,
        "first_name": id_info.given_name,
        "last_name": id_info.family_name,
        "is_staff": id_info.email in settings.GOOGLE_SSO_STAFF_LIST,
        "is_superuser": id_info.email in settings.GOOGLE_SSO_SUPERUSER_LIST,
        "is_active": True,
        "email_verified": True,
        "language": language,
    }
    if settings.GOOGLE_SSO_ALWAYS_UPDATE_USER_DATA:
        user, created = RevelUser.objects.update_or_create(
            username=id_info.email,
            defaults=defaults,
            create_defaults=defaults,
        )
    else:
        user, created = RevelUser.objects.get_or_create(
            username=id_info.email,
            defaults=defaults,
        )
    if created:
        logger.info("google_sso_user_created", user_id=str(user.id), email=user.email, google_id=id_info.sub)
        GoogleSSOUser.objects.create(
            user=user,
            google_id=id_info.sub,
            picture_url=id_info.picture,
            locale=id_info.locale,
        )
    else:
        logger.info("google_sso_login", user_id=str(user.id), email=user.email)
    return get_token_pair_for_user(user)


def verify_oauth2_token(id_token: str) -> schema.GoogleIDInfo:
    """Async wrapper for verifying Google OAuth2 token."""
    try:
        id_info = _verify_oauth2_token(
            id_token,
            Request(),  # type: ignore[no-untyped-call]
            settings.GOOGLE_SSO_CLIENT_ID,
        )
        validated_info = schema.GoogleIDInfo.model_validate(id_info)
        logger.info("google_token_verified", email=validated_info.email)
        return validated_info
    except GoogleAuthError as e:
        logger.warning("google_token_verification_failed", error=str(e))
        raise HttpError(401, str(_("Invalid Google ID Token."))) from e

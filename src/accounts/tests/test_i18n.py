"""Tests for internationalization (i18n) functionality."""

from unittest.mock import patch

import pytest
from django.test import RequestFactory, override_settings
from django.utils import translation
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from accounts.schema import RegisterUserSchema
from accounts.service.account import register_user, token_to_payload
from common.authentication import I18nJWTAuth, OptionalAuth

pytestmark = pytest.mark.django_db


def test_user_language_field_choices(user: RevelUser) -> None:
    """Test that user language field has correct language choices."""
    # Language choices should be: en, de, it
    valid_languages = ["en", "de", "it"]
    assert user.language in valid_languages


@patch("accounts.tasks.send_verification_email.delay")
def test_error_message_translation_german(mock_send_email: object, user: RevelUser) -> None:
    """Test that error messages are translated to German."""
    with translation.override("de"):
        with pytest.raises(HttpError, match="existiert bereits"):
            register_user(
                RegisterUserSchema(
                    email=user.email,
                    password1="TestPass123!",
                    password2="TestPass123!",
                    first_name="Test",
                    last_name="User",
                )
            )


@patch("accounts.tasks.send_verification_email.delay")
def test_error_message_translation_italian(mock_send_email: object, user: RevelUser) -> None:
    """Test that error messages are translated to Italian."""
    with translation.override("it"):
        with pytest.raises(HttpError, match="Esiste già"):
            register_user(
                RegisterUserSchema(
                    email=user.email,
                    password1="TestPass123!",
                    password2="TestPass123!",
                    first_name="Test",
                    last_name="User",
                )
            )


@patch("accounts.tasks.send_verification_email.delay")
def test_error_message_default_english(mock_send_email: object, user: RevelUser) -> None:
    """Test that error messages default to English when no translation is active."""
    with translation.override("en"):
        with pytest.raises(HttpError, match="already exists"):
            register_user(
                RegisterUserSchema(
                    email=user.email,
                    password1="TestPass123!",
                    password2="TestPass123!",
                    first_name="Test",
                    last_name="User",
                )
            )


def test_token_expiry_error_translation_german() -> None:
    """Test that token expiry errors are translated to German."""
    import jwt
    from django.conf import settings

    from accounts.schema import VerifyEmailJWTPayloadSchema

    # Create an expired token
    expired_token = jwt.encode({"exp": 0}, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    with translation.override("de"):
        with pytest.raises(HttpError, match="abgelaufen"):
            token_to_payload(expired_token, VerifyEmailJWTPayloadSchema)


def test_token_expiry_error_translation_italian() -> None:
    """Test that token expiry errors are translated to Italian."""
    import jwt
    from django.conf import settings

    from accounts.schema import VerifyEmailJWTPayloadSchema

    # Create an expired token
    expired_token = jwt.encode({"exp": 0}, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

    with translation.override("it"):
        with pytest.raises(HttpError, match="scaduto"):
            token_to_payload(expired_token, VerifyEmailJWTPayloadSchema)


@override_settings(LANGUAGE_CODE="de")
def test_language_setting_affects_default() -> None:
    """Test that LANGUAGE_CODE setting affects the default language."""
    from django.utils.translation import gettext_lazy as _

    # This should be German because of @override_settings
    translated = str(_("Email already verified."))
    assert "verifiziert" in translated.lower()


def test_translation_override_context_manager() -> None:
    """Test that translation.override context manager works correctly."""
    from django.utils.translation import gettext_lazy as _

    message = _("Invalid OTP.")

    # Test German
    with translation.override("de"):
        german_message = str(message)
        assert "ungültig" in german_message.lower()

    # Test Italian
    with translation.override("it"):
        italian_message = str(message)
        assert "valido" in italian_message.lower()

    # Test English
    with translation.override("en"):
        english_message = str(message)
        assert "invalid" in english_message.lower()


def test_i18n_jwt_auth_activates_user_language(
    django_user_model: type[RevelUser],
) -> None:
    """Test that I18nJWTAuth activates the user's preferred language."""
    # Create users with different language preferences
    user_de = django_user_model.objects.create_user(
        username="german@test.com",
        email="german@test.com",
        password="password",
        language="de",
    )
    user_it = django_user_model.objects.create_user(
        username="italian@test.com",
        email="italian@test.com",
        password="password",
        language="it",
    )

    # Generate JWT tokens for each user
    token_de = str(RefreshToken.for_user(user_de).access_token)  # type: ignore[attr-defined]
    token_it = str(RefreshToken.for_user(user_it).access_token)  # type: ignore[attr-defined]

    # Create mock requests
    factory = RequestFactory()

    # Test German user
    request_de = factory.get("/test")
    auth = I18nJWTAuth()
    auth.authenticate(request_de, token_de)

    # Check that German language is activated
    assert translation.get_language() == "de"
    assert hasattr(request_de, "LANGUAGE_CODE") and request_de.LANGUAGE_CODE == "de"

    # Test Italian user
    request_it = factory.get("/test")
    auth.authenticate(request_it, token_it)

    # Check that Italian language is activated
    assert translation.get_language() == "it"
    assert hasattr(request_it, "LANGUAGE_CODE") and request_it.LANGUAGE_CODE == "it"


def test_i18n_jwt_auth_with_translated_response(
    django_user_model: type[RevelUser],
) -> None:
    """Test that responses are translated based on JWT user's language."""
    from django.utils.translation import gettext_lazy as _

    # Create users with different languages
    user_de = django_user_model.objects.create_user(
        username="de@test.com",
        email="de@test.com",
        password="password",
        language="de",
    )
    user_it = django_user_model.objects.create_user(
        username="it@test.com",
        email="it@test.com",
        password="password",
        language="it",
    )

    token_de = str(RefreshToken.for_user(user_de).access_token)  # type: ignore[attr-defined]
    token_it = str(RefreshToken.for_user(user_it).access_token)  # type: ignore[attr-defined]

    factory = RequestFactory()
    auth = I18nJWTAuth()

    # Test with German user
    request_de = factory.get("/test")
    auth.authenticate(request_de, token_de)
    message = str(_("Email already verified."))
    assert "verifiziert" in message.lower()

    # Test with Italian user
    request_it = factory.get("/test")
    auth.authenticate(request_it, token_it)
    message = str(_("Email already verified."))
    assert "verificat" in message.lower()


def test_optional_auth_with_token_activates_language(
    django_user_model: type[RevelUser],
) -> None:
    """Test that OptionalAuth activates language when token is provided."""
    user_de = django_user_model.objects.create_user(
        username="deuser@test.com",
        email="deuser@test.com",
        password="password",
        language="de",
    )
    token_de = str(RefreshToken.for_user(user_de).access_token)  # type: ignore[attr-defined]

    factory = RequestFactory()
    request = factory.get("/test", HTTP_AUTHORIZATION=f"Bearer {token_de}")

    auth = OptionalAuth()
    result = auth(request)

    # Should authenticate the user
    assert result is not None
    assert request.user.id == user_de.id

    # Should activate German language
    assert translation.get_language() == "de"


def test_optional_auth_without_token_allows_anonymous() -> None:
    """Test that OptionalAuth allows anonymous users when no token provided."""
    from django.contrib.auth.models import AnonymousUser

    factory = RequestFactory()
    request = factory.get("/test")  # No Authorization header

    auth = OptionalAuth()
    result = auth(request)

    # Should set AnonymousUser
    assert isinstance(result, AnonymousUser)
    assert request.user.is_anonymous

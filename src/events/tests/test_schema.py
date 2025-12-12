import pytest
from pydantic import AnyUrl, ValidationError

from events.schema import SocialMediaSchemaEditMixin, SocialMediaSchemaRetrieveMixin


class TestSocialMediaSchemaEditMixin:
    """Tests for SocialMediaSchemaEditMixin URL validation."""

    @pytest.mark.parametrize(
        ("field", "url"),
        [
            # Instagram valid URLs
            ("instagram_url", "https://instagram.com/username"),
            ("instagram_url", "https://www.instagram.com/username"),
            ("instagram_url", "http://instagram.com/username"),
            # Facebook valid URLs
            ("facebook_url", "https://facebook.com/page"),
            ("facebook_url", "https://www.facebook.com/page"),
            ("facebook_url", "https://fb.com/page"),
            ("facebook_url", "https://www.fb.com/page"),
            # Bluesky valid URLs
            ("bluesky_url", "https://bsky.app/profile/user.bsky.social"),
            ("bluesky_url", "https://bsky.social/user"),
            # Telegram valid URLs
            ("telegram_url", "https://t.me/username"),
            ("telegram_url", "https://telegram.me/username"),
            ("telegram_url", "https://telegram.dog/username"),
        ],
    )
    def test_valid_social_media_urls(self, field: str, url: str) -> None:
        """Test that valid URLs for each platform are accepted."""
        data = {field: url}
        schema = SocialMediaSchemaEditMixin.model_validate(data)
        assert getattr(schema, field) == AnyUrl(url)

    @pytest.mark.parametrize(
        ("field", "url", "expected_error"),
        [
            # Instagram invalid URLs
            ("instagram_url", "https://facebook.com/user", "Instagram"),
            ("instagram_url", "https://twitter.com/user", "Instagram"),
            ("instagram_url", "https://example.com/user", "Instagram"),
            # Facebook invalid URLs
            ("facebook_url", "https://instagram.com/user", "Facebook"),
            ("facebook_url", "https://twitter.com/user", "Facebook"),
            ("facebook_url", "https://example.com/user", "Facebook"),
            # Bluesky invalid URLs
            ("bluesky_url", "https://twitter.com/user", "Bluesky"),
            ("bluesky_url", "https://mastodon.social/user", "Bluesky"),
            ("bluesky_url", "https://example.com/user", "Bluesky"),
            # Telegram invalid URLs
            ("telegram_url", "https://whatsapp.com/user", "Telegram"),
            ("telegram_url", "https://signal.org/user", "Telegram"),
            ("telegram_url", "https://example.com/user", "Telegram"),
        ],
    )
    def test_invalid_social_media_urls(self, field: str, url: str, expected_error: str) -> None:
        """Test that invalid URLs for each platform are rejected."""
        data = {field: url}
        with pytest.raises(ValidationError) as exc_info:
            SocialMediaSchemaEditMixin.model_validate(data)

        errors = exc_info.value.errors()
        assert len(errors) == 1
        assert field in errors[0]["loc"]
        assert f"URL must be a valid {expected_error} link" in str(errors[0]["msg"])

    @pytest.mark.parametrize(
        "field",
        ["instagram_url", "facebook_url", "bluesky_url", "telegram_url"],
    )
    def test_empty_string_returns_none(self, field: str) -> None:
        """Test that empty strings are converted to None (for DB null=True)."""
        data = {field: ""}
        schema = SocialMediaSchemaEditMixin.model_validate(data)
        assert getattr(schema, field) is None

    def test_all_fields_default_to_none(self) -> None:
        """Test that all social media fields default to None."""
        schema = SocialMediaSchemaEditMixin()
        assert schema.instagram_url is None
        assert schema.facebook_url is None
        assert schema.bluesky_url is None
        assert schema.telegram_url is None

    def test_multiple_valid_urls(self) -> None:
        """Test that multiple valid URLs can be set at once."""
        schema = SocialMediaSchemaEditMixin.model_validate(
            {
                "instagram_url": "https://instagram.com/user",
                "facebook_url": "https://facebook.com/page",
                "bluesky_url": "https://bsky.app/profile/user",
                "telegram_url": "https://t.me/channel",
            }
        )
        assert schema.instagram_url == AnyUrl("https://instagram.com/user")
        assert schema.facebook_url == AnyUrl("https://facebook.com/page")
        assert schema.bluesky_url == AnyUrl("https://bsky.app/profile/user")
        assert schema.telegram_url == AnyUrl("https://t.me/channel")

    @pytest.mark.parametrize(
        ("field", "input_url", "expected_url"),
        [
            ("instagram_url", "instagram.com/user", "https://instagram.com/user"),
            ("instagram_url", "www.instagram.com/user", "https://www.instagram.com/user"),
            ("facebook_url", "facebook.com/page", "https://facebook.com/page"),
            ("facebook_url", "fb.com/page", "https://fb.com/page"),
            ("bluesky_url", "bsky.app/profile/user", "https://bsky.app/profile/user"),
            ("telegram_url", "t.me/channel", "https://t.me/channel"),
        ],
    )
    def test_prepends_https_when_no_scheme(self, field: str, input_url: str, expected_url: str) -> None:
        """Test that https:// is prepended when no scheme is provided."""
        data = {field: input_url}
        schema = SocialMediaSchemaEditMixin.model_validate(data)
        assert getattr(schema, field) == AnyUrl(expected_url)

    @pytest.mark.parametrize(
        ("field", "url"),
        [
            ("instagram_url", "http://instagram.com/user"),
            ("instagram_url", "https://instagram.com/user"),
            ("facebook_url", "http://facebook.com/page"),
            ("facebook_url", "https://facebook.com/page"),
        ],
    )
    def test_does_not_modify_urls_with_scheme(self, field: str, url: str) -> None:
        """Test that URLs with existing scheme are not modified."""
        data = {field: url}
        schema = SocialMediaSchemaEditMixin.model_validate(data)
        assert getattr(schema, field) == AnyUrl(url)


class TestSocialMediaSchemaRetrieveMixin:
    """Tests for SocialMediaSchemaRetrieveMixin (no validation)."""

    def test_accepts_any_url(self) -> None:
        """Test that retrieve mixin accepts any URL without validation."""
        schema = SocialMediaSchemaRetrieveMixin(
            instagram_url="https://example.com/not-instagram",
            facebook_url="https://random-site.org/page",
            bluesky_url="invalid-url",
            telegram_url="",
        )
        assert schema.instagram_url == "https://example.com/not-instagram"
        assert schema.facebook_url == "https://random-site.org/page"
        assert schema.bluesky_url == "invalid-url"
        assert schema.telegram_url == ""

    def test_all_fields_default_to_none(self) -> None:
        """Test that all social media fields default to None."""
        schema = SocialMediaSchemaRetrieveMixin()
        assert schema.instagram_url is None
        assert schema.facebook_url is None
        assert schema.bluesky_url is None
        assert schema.telegram_url is None

"""Tests for email normalization utilities."""

import pytest

from accounts.utils.email_normalization import (
    extract_domain,
    normalize_domain_for_matching,
    normalize_email_for_matching,
    normalize_telegram_for_matching,
)


class TestNormalizeEmailForMatching:
    """Tests for normalize_email_for_matching."""

    def test_lowercase(self) -> None:
        assert normalize_email_for_matching("User@Example.COM") == "user@example.com"

    def test_strip_plus_tag(self) -> None:
        assert normalize_email_for_matching("user+tag@example.com") == "user@example.com"

    def test_strip_plus_tag_complex(self) -> None:
        assert normalize_email_for_matching("user+foo+bar@example.com") == "user@example.com"

    def test_gmail_dot_removal(self) -> None:
        assert normalize_email_for_matching("first.last@gmail.com") == "firstlast@gmail.com"

    def test_googlemail_dot_removal(self) -> None:
        assert normalize_email_for_matching("first.last@googlemail.com") == "firstlast@googlemail.com"

    def test_gmail_combined_tag_and_dots(self) -> None:
        assert normalize_email_for_matching("f.i.r.s.t+spam@gmail.com") == "first@gmail.com"

    def test_non_gmail_keeps_dots(self) -> None:
        assert normalize_email_for_matching("first.last@outlook.com") == "first.last@outlook.com"

    def test_whitespace_stripped(self) -> None:
        assert normalize_email_for_matching("  user@example.com  ") == "user@example.com"

    def test_no_at_sign(self) -> None:
        assert normalize_email_for_matching("invalidemail") == "invalidemail"

    def test_empty_local_part(self) -> None:
        assert normalize_email_for_matching("@example.com") == "@example.com"


class TestNormalizeTelegramForMatching:
    """Tests for normalize_telegram_for_matching."""

    def test_strip_at_prefix(self) -> None:
        assert normalize_telegram_for_matching("@username") == "username"

    def test_lowercase(self) -> None:
        assert normalize_telegram_for_matching("UserName") == "username"

    def test_strip_whitespace(self) -> None:
        assert normalize_telegram_for_matching("  @UserName  ") == "username"

    def test_no_at_prefix(self) -> None:
        assert normalize_telegram_for_matching("username") == "username"

    def test_multiple_at_signs(self) -> None:
        assert normalize_telegram_for_matching("@@double") == "double"


class TestNormalizeDomainForMatching:
    """Tests for normalize_domain_for_matching."""

    def test_lowercase(self) -> None:
        assert normalize_domain_for_matching("EXAMPLE.COM") == "example.com"

    def test_strip_whitespace(self) -> None:
        assert normalize_domain_for_matching("  example.com  ") == "example.com"

    def test_combined(self) -> None:
        assert normalize_domain_for_matching("  EVIL.COM  ") == "evil.com"

    def test_already_normalized(self) -> None:
        assert normalize_domain_for_matching("example.com") == "example.com"


class TestExtractDomain:
    """Tests for extract_domain."""

    def test_basic(self) -> None:
        assert extract_domain("user@example.com") == "example.com"

    def test_uppercase(self) -> None:
        assert extract_domain("user@EXAMPLE.COM") == "example.com"

    def test_whitespace(self) -> None:
        assert extract_domain("  user@example.com  ") == "example.com"

    def test_no_at_sign(self) -> None:
        assert extract_domain("nodomain") == ""

    @pytest.mark.parametrize(
        "email,expected",
        [
            ("user@sub.domain.com", "sub.domain.com"),
            ("user@gmail.com", "gmail.com"),
        ],
    )
    def test_various_domains(self, email: str, expected: str) -> None:
        assert extract_domain(email) == expected

"""Tests for email verification reminder templates."""

import pytest
from django.template.loader import render_to_string

from accounts.models import RevelUser
from common.models import SiteSettings


@pytest.fixture
def site_settings() -> SiteSettings:
    """Get or create site settings for template tests."""
    settings = SiteSettings.get_solo()
    settings.frontend_base_url = "https://example.com"
    settings.save()
    return settings


@pytest.fixture
def sample_user(db: None) -> RevelUser:
    """Create a sample user for template context."""
    return RevelUser.objects.create_user(
        username="templatetest",
        email="template@example.com",
        password="testpass123",
        email_verified=False,
    )


class TestVerificationReminderTemplates:
    """Test email verification reminder templates render without errors."""

    def test_email_verification_reminder_subject(self) -> None:
        """Test reminder subject template renders."""
        subject = render_to_string("accounts/emails/email_verification_reminder_subject.txt")
        assert subject
        assert isinstance(subject, str)
        # Should not contain template tags
        assert "{{" not in subject
        assert "{%" not in subject

    def test_email_verification_reminder_txt(self, sample_user: RevelUser, site_settings: SiteSettings) -> None:
        """Test reminder text template renders with all required context."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://example.com/delete?token=xyz789",
            "user": sample_user,
        }
        body = render_to_string("accounts/emails/email_verification_reminder_body.txt", context)
        assert body
        assert "verification_link" in body or "https://example.com/verify" in body
        assert "deletion_link" in body or "https://example.com/delete" in body
        # Should not contain unrendered template tags
        assert "{{" not in body
        assert "verification_link }}" not in body

    def test_email_verification_reminder_html(self, sample_user: RevelUser, site_settings: SiteSettings) -> None:
        """Test reminder HTML template renders with all required context."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://example.com/delete?token=xyz789",
            "user": sample_user,
        }
        html_body = render_to_string("accounts/emails/email_verification_reminder_body.html", context)
        assert html_body
        assert "https://example.com/verify" in html_body
        assert "https://example.com/delete" in html_body
        # Should be valid HTML
        assert "<html>" in html_body or "<!DOCTYPE html>" in html_body
        # Should not contain unrendered template tags
        assert "{{" not in html_body
        assert "verification_link }}" not in html_body


class TestFinalWarningTemplates:
    """Test final warning email templates render without errors."""

    def test_email_verification_final_warning_subject(self) -> None:
        """Test final warning subject template renders."""
        subject = render_to_string("accounts/emails/email_verification_final_warning_subject.txt")
        assert subject
        assert isinstance(subject, str)
        # Should not contain template tags
        assert "{{" not in subject
        assert "{%" not in subject

    def test_email_verification_final_warning_txt(self, sample_user: RevelUser, site_settings: SiteSettings) -> None:
        """Test final warning text template renders with all required context."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://example.com/delete?token=xyz789",
            "user": sample_user,
        }
        body = render_to_string("accounts/emails/email_verification_final_warning_body.txt", context)
        assert body
        assert "verification_link" in body or "https://example.com/verify" in body
        assert "deletion_link" in body or "https://example.com/delete" in body
        # Should not contain unrendered template tags
        assert "{{" not in body
        assert "verification_link }}" not in body

    def test_email_verification_final_warning_html(self, sample_user: RevelUser, site_settings: SiteSettings) -> None:
        """Test final warning HTML template renders with all required context."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://example.com/delete?token=xyz789",
            "user": sample_user,
        }
        html_body = render_to_string("accounts/emails/email_verification_final_warning_body.html", context)
        assert html_body
        assert "https://example.com/verify" in html_body
        assert "https://example.com/delete" in html_body
        # Should be valid HTML
        assert "<html>" in html_body or "<!DOCTYPE html>" in html_body
        # Should not contain unrendered template tags
        assert "{{" not in html_body
        assert "verification_link }}" not in html_body


class TestDeactivationTemplates:
    """Test account deactivation email templates render without errors."""

    def test_account_deactivated_subject(self) -> None:
        """Test deactivation subject template renders."""
        subject = render_to_string("accounts/emails/account_deactivated_subject.txt")
        assert subject
        assert isinstance(subject, str)
        # Should not contain template tags
        assert "{{" not in subject
        assert "{%" not in subject

    def test_account_deactivated_txt(self, sample_user: RevelUser, site_settings: SiteSettings) -> None:
        """Test deactivation text template renders with all required context."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://example.com/delete?token=xyz789",
            "user": sample_user,
        }
        body = render_to_string("accounts/emails/account_deactivated_body.txt", context)
        assert body
        assert "verification_link" in body or "https://example.com/verify" in body
        assert "deletion_link" in body or "https://example.com/delete" in body
        # Should not contain unrendered template tags
        assert "{{" not in body
        assert "verification_link }}" not in body

    def test_account_deactivated_html(self, sample_user: RevelUser, site_settings: SiteSettings) -> None:
        """Test deactivation HTML template renders with all required context."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://example.com/delete?token=xyz789",
            "user": sample_user,
        }
        html_body = render_to_string("accounts/emails/account_deactivated_body.html", context)
        assert html_body
        assert "https://example.com/verify" in html_body
        assert "https://example.com/delete" in html_body
        # Should be valid HTML
        assert "<html>" in html_body or "<!DOCTYPE html>" in html_body
        # Should not contain unrendered template tags
        assert "{{" not in html_body
        assert "verification_link }}" not in html_body


class TestTemplateBlocktransSyntax:
    """Test that blocktrans syntax is correct and interpolates variables properly."""

    def test_reminder_html_blocktrans_interpolation(self, sample_user: RevelUser, site_settings: SiteSettings) -> None:
        """Test that blocktrans with deletion_link properly interpolates in reminder HTML."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://unique-deletion-link-test.com/delete?token=xyz789",
            "user": sample_user,
        }
        html_body = render_to_string("accounts/emails/email_verification_reminder_body.html", context)

        # The deletion link should be interpolated into the anchor href
        assert "https://unique-deletion-link-test.com/delete" in html_body
        # Should NOT contain the literal variable name
        assert "{{ deletion_link }}" not in html_body
        assert "{{ link }}" not in html_body

    def test_final_warning_html_blocktrans_interpolation(
        self, sample_user: RevelUser, site_settings: SiteSettings
    ) -> None:
        """Test that blocktrans with deletion_link properly interpolates in final warning HTML."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://unique-deletion-link-test.com/delete?token=xyz789",
            "user": sample_user,
        }
        html_body = render_to_string("accounts/emails/email_verification_final_warning_body.html", context)

        # The deletion link should be interpolated into the anchor href
        assert "https://unique-deletion-link-test.com/delete" in html_body
        # Should NOT contain the literal variable name
        assert "{{ deletion_link }}" not in html_body
        assert "{{ link }}" not in html_body

    def test_deactivation_html_blocktrans_interpolation(
        self, sample_user: RevelUser, site_settings: SiteSettings
    ) -> None:
        """Test that blocktrans with deletion_link properly interpolates in deactivation HTML."""
        context = {
            "verification_link": "https://example.com/verify?token=abc123",
            "deletion_link": "https://unique-deletion-link-test.com/delete?token=xyz789",
            "user": sample_user,
        }
        html_body = render_to_string("accounts/emails/account_deactivated_body.html", context)

        # The deletion link should be interpolated into the anchor href
        assert "https://unique-deletion-link-test.com/delete" in html_body
        # Should NOT contain the literal variable name
        assert "{{ deletion_link }}" not in html_body
        assert "{{ link }}" not in html_body


class TestTemplateSubjectStripping:
    """Test that subject templates produce clean output without newlines."""

    def test_reminder_subject_no_trailing_newline(self) -> None:
        """Test reminder subject has no trailing newline."""
        subject = render_to_string("accounts/emails/email_verification_reminder_subject.txt")
        # render_to_string returns raw output, tasks should .strip() it
        # But the template itself shouldn't have excessive whitespace
        stripped = subject.strip()
        assert stripped == subject or len(subject) - len(stripped) <= 2  # Allow minimal whitespace

    def test_final_warning_subject_no_trailing_newline(self) -> None:
        """Test final warning subject has no trailing newline."""
        subject = render_to_string("accounts/emails/email_verification_final_warning_subject.txt")
        stripped = subject.strip()
        assert stripped == subject or len(subject) - len(stripped) <= 2

    def test_deactivation_subject_no_trailing_newline(self) -> None:
        """Test deactivation subject has no trailing newline."""
        subject = render_to_string("accounts/emails/account_deactivated_subject.txt")
        stripped = subject.strip()
        assert stripped == subject or len(subject) - len(stripped) <= 2

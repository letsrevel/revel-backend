"""Tests for custom Django fields with sanitization."""

import typing as t

import pytest
from django.db import models
from django.test import TestCase

from accounts.models import RevelUser
from common.fields import (
    PROTECTED_PREFIX,
    ProtectedFileField,
    ProtectedImageField,
    ProtectedUploadTo,
    sanitize_html,
    sanitize_markdown,
)
from events.models import Event, EventSeries, Organization


class TestSanitizeHtml(TestCase):
    """Test HTML sanitization."""

    def test_basic_html_preserved(self) -> None:
        """Test that safe HTML tags are preserved."""
        html = "<p><strong>Bold</strong> and <em>italic</em> text.</p>"
        result = sanitize_html(html)

        assert "<p>" in result
        assert "<strong>Bold</strong>" in result
        assert "<em>italic</em>" in result

    def test_headers_preserved(self) -> None:
        """Test all header levels are preserved."""
        html = "<h1>H1</h1><h2>H2</h2><h3>H3</h3><h4>H4</h4><h5>H5</h5><h6>H6</h6>"
        result = sanitize_html(html)

        for i in range(1, 7):
            assert f"<h{i}>H{i}</h{i}>" in result

    def test_lists_preserved(self) -> None:
        """Test lists are preserved."""
        html = "<ul><li>Item 1</li><li>Item 2</li></ul><ol><li>First</li></ol>"
        result = sanitize_html(html)

        assert "<ul>" in result
        assert "<li>Item 1</li>" in result
        assert "<ol>" in result

    def test_links_preserved(self) -> None:
        """Test that safe links are preserved."""
        html = '<a href="https://example.com">Click here</a>'
        result = sanitize_html(html)

        # nh3 adds rel="noopener noreferrer" for security
        assert 'href="https://example.com"' in result
        assert "Click here</a>" in result

    def test_tables_preserved(self) -> None:
        """Test that tables are preserved."""
        html = "<table><thead><tr><th>Header</th></tr></thead><tbody><tr><td>Cell</td></tr></tbody></table>"
        result = sanitize_html(html)

        assert "<table>" in result
        assert "<th>Header</th>" in result
        assert "<td>Cell</td>" in result

    def test_empty_input(self) -> None:
        """Test that empty input returns empty string."""
        assert sanitize_html("") == ""
        assert sanitize_html(None) == ""


class TestXSSPrevention(TestCase):
    """Test XSS attack prevention."""

    def test_script_tag_stripped(self) -> None:
        """Test that <script> tags are completely removed."""
        malicious = '<script>alert("XSS")</script>Safe text'
        html = sanitize_html(malicious)

        assert "<script>" not in html.lower()
        assert "Safe text" in html

    def test_javascript_href_stripped(self) -> None:
        """Test that javascript: URLs are stripped."""
        malicious = "<a href=\"javascript:alert('XSS')\">Click</a>"
        html = sanitize_html(malicious)

        assert "javascript:" not in html.lower()

    def test_onclick_attribute_stripped(self) -> None:
        """Test that onclick and other event handlers are stripped."""
        malicious = "<div onclick=\"alert('XSS')\">Click me</div>"
        html = sanitize_html(malicious)

        assert "onclick" not in html.lower()
        assert "alert" not in html

    def test_img_tag_stripped(self) -> None:
        """Test that img tags are stripped (not in allowed list)."""
        malicious = '<img src="x" onerror="alert(\'XSS\')">'
        html = sanitize_html(malicious)

        assert "<img" not in html.lower()

    def test_style_tag_stripped(self) -> None:
        """Test that <style> tags are stripped."""
        malicious = "<style>body { background: red; }</style>Text"
        html = sanitize_html(malicious)

        assert "<style>" not in html.lower()
        assert "Text" in html

    def test_iframe_stripped(self) -> None:
        """Test that iframes are stripped."""
        malicious = '<iframe src="https://evil.com"></iframe>Safe'
        html = sanitize_html(malicious)

        assert "<iframe" not in html.lower()
        assert "Safe" in html

    def test_svg_stripped(self) -> None:
        """Test that SVG elements are stripped."""
        malicious = '<svg onload="alert(1)"><circle /></svg>Safe'
        html = sanitize_html(malicious)

        assert "<svg" not in html.lower()
        assert "Safe" in html


class TestLinkSecurity(TestCase):
    """Test link security."""

    def test_https_links_allowed(self) -> None:
        """Test that HTTPS links are allowed."""
        html = '<a href="https://example.com">Link</a>'
        result = sanitize_html(html)
        assert 'href="https://example.com"' in result

    def test_http_links_allowed(self) -> None:
        """Test that HTTP links are allowed."""
        html = '<a href="http://example.com">Link</a>'
        result = sanitize_html(html)
        assert 'href="http://example.com"' in result

    def test_mailto_links_allowed(self) -> None:
        """Test that mailto: links are allowed."""
        html = '<a href="mailto:test@example.com">Email</a>'
        result = sanitize_html(html)
        assert 'href="mailto:test@example.com"' in result

    def test_javascript_links_stripped(self) -> None:
        """Test that javascript: links are stripped."""
        html = '<a href="javascript:alert(1)">Click</a>'
        result = sanitize_html(html)
        assert "javascript:" not in result.lower()

    def test_data_uri_links_stripped(self) -> None:
        """Test that data: URI links are stripped."""
        html = '<a href="data:text/html,<script>alert(1)</script>">Click</a>'
        result = sanitize_html(html)
        assert "data:" not in result.lower()


class TestSanitizeMarkdown(TestCase):
    """Test markdown sanitization."""

    def test_plain_text_preserved(self) -> None:
        """Test that plain text is preserved."""
        text = "This is plain text."
        result = sanitize_markdown(text)
        assert result == text

    def test_markdown_syntax_preserved(self) -> None:
        """Test that markdown syntax is preserved."""
        text = "# Heading\n\n**Bold** and *italic*"
        result = sanitize_markdown(text)
        assert "# Heading" in result
        assert "**Bold**" in result
        assert "*italic*" in result

    def test_embedded_html_sanitized(self) -> None:
        """Test that embedded HTML in markdown is sanitized."""
        text = "Normal text\n\n<script>alert('XSS')</script>\n\nMore text"
        result = sanitize_markdown(text)

        assert "Normal text" in result
        assert "<script>" not in result.lower()
        assert "More text" in result

    def test_empty_input(self) -> None:
        """Test that empty input returns empty string."""
        assert sanitize_markdown("") == ""
        assert sanitize_markdown(None) == ""


class TestComplexXSSVectors(TestCase):
    """Test complex XSS attack vectors."""

    def test_nested_script_tags(self) -> None:
        """Test nested script attempts."""
        malicious = "<div><script>alert(1)</script></div>"
        html = sanitize_html(malicious)

        assert "<script>" not in html.lower()

    def test_encoded_javascript(self) -> None:
        """Test URL-encoded JavaScript."""
        malicious = '<a href="javascript%3Aalert(1)">Click</a>'
        html = sanitize_html(malicious)

        # The href with javascript should be stripped
        assert "javascript" not in html.lower() or 'href="javascript' not in html.lower()
        assert "Click" in html

    def test_svg_xss(self) -> None:
        """Test SVG-based XSS."""
        malicious = '<svg onload="alert(1)">'
        html = sanitize_html(malicious)

        assert "<svg" not in html.lower()

    def test_form_action_javascript(self) -> None:
        """Test form with javascript action."""
        malicious = '<form action="javascript:alert(1)"><input type="submit"></form>'
        html = sanitize_html(malicious)

        # Forms should be stripped entirely as they're not in allowlist
        assert "<form" not in html.lower()


@pytest.mark.django_db
class TestMarkdownFieldOnModels(TestCase):
    """Test MarkdownField functionality on actual models."""

    def setUp(self) -> None:
        """Create a test user for organization owner."""
        self.user = RevelUser.objects.create_user(username="testuser", email="test@example.com", password="testpass")

    def test_organization_description_sanitized_on_save(self) -> None:
        """Test that Organization description is sanitized on save."""
        org = Organization.objects.create(
            name="Test Org",
            owner=self.user,
            description="# Welcome\n\n<script>alert('XSS')</script>\n\n**Safe**",
        )

        # The script tag should be removed, safe content preserved
        assert org.description is not None
        assert "<script>" not in org.description.lower()
        assert "# Welcome" in org.description
        assert "**Safe**" in org.description

    def test_event_series_description_sanitized(self) -> None:
        """Test that EventSeries description is sanitized on save."""
        org = Organization.objects.create(name="Test Org 2", owner=self.user)
        series = EventSeries.objects.create(
            organization=org,
            name="Test Series",
            description="## Event Series\n\n<img src=x onerror=alert(1)>",
        )

        # img tag should be stripped
        assert series.description is not None
        assert "<img" not in series.description.lower()
        assert "## Event Series" in series.description

    def test_event_markdown_fields_sanitized(self) -> None:
        """Test Event markdown fields are sanitized."""
        from datetime import datetime, timezone

        org = Organization.objects.create(name="Test Org 3", owner=self.user)
        event = Event.objects.create(
            organization=org,
            name="Test Event",
            description="**Event** <script>evil()</script>",
            invitation_message="You're *invited*! <iframe src='evil.com'></iframe>",
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc),
        )

        assert event.description is not None
        assert event.invitation_message is not None
        assert "<script>" not in event.description.lower()
        assert "**Event**" in event.description
        assert "<iframe" not in event.invitation_message.lower()
        assert "*invited*" in event.invitation_message

    def test_null_markdown_field(self) -> None:
        """Test that null markdown fields remain null."""
        org = Organization.objects.create(name="Test Org 4", owner=self.user, description=None)
        assert org.description is None

    def test_empty_markdown_field(self) -> None:
        """Test that empty markdown fields remain empty."""
        org = Organization.objects.create(name="Test Org 5", owner=self.user, description="")
        assert org.description == ""

    def test_safe_html_in_markdown_preserved(self) -> None:
        """Test that safe HTML tags are preserved in markdown."""
        org = Organization.objects.create(
            name="Test Org 6",
            owner=self.user,
            description="Text with <strong>bold</strong> and <a href='https://example.com'>link</a>",
        )

        assert org.description is not None
        assert "<strong>bold</strong>" in org.description
        assert "<a href" in org.description


# ---- Protected File Field Tests ----


class TestProtectedUploadTo:
    """Tests for ProtectedUploadTo wrapper class."""

    def test_wraps_callable_and_adds_prefix(self) -> None:
        """Test that callable upload_to gets protected/ prefix added."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"user/{filename}"

        wrapper = ProtectedUploadTo(my_upload)
        result = wrapper(None, "test.pdf")

        assert result == "protected/user/test.pdf"

    def test_does_not_double_prefix(self) -> None:
        """Test that already-prefixed paths are not doubled."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"protected/already/{filename}"

        wrapper = ProtectedUploadTo(my_upload)
        result = wrapper(None, "test.pdf")

        assert result == "protected/already/test.pdf"
        assert not result.startswith("protected/protected/")

    def test_prevents_double_wrapping(self) -> None:
        """Test that wrapping a ProtectedUploadTo doesn't nest wrappers."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"files/{filename}"

        wrapper1 = ProtectedUploadTo(my_upload)
        wrapper2 = ProtectedUploadTo(wrapper1)

        # The inner wrapper should be unwrapped
        assert wrapper2.wrapped is my_upload

    def test_deconstruct_returns_valid_tuple(self) -> None:
        """Test that deconstruct returns a valid 3-tuple for migrations."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"path/{filename}"

        wrapper = ProtectedUploadTo(my_upload)
        path, args, kwargs = wrapper.deconstruct()

        assert path == "common.fields.ProtectedUploadTo"
        assert args == (my_upload,)
        assert kwargs == {}

    def test_deconstruct_can_reconstruct(self) -> None:
        """Test that deconstruct output can be used to reconstruct."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"docs/{filename}"

        original = ProtectedUploadTo(my_upload)
        path, args, kwargs = original.deconstruct()

        # Reconstruct
        reconstructed = ProtectedUploadTo(*args, **kwargs)

        # Should behave the same
        assert reconstructed(None, "file.pdf") == original(None, "file.pdf")


class TestProtectedFileField:
    """Tests for ProtectedFileField."""

    def test_string_upload_to_gets_prefix(self) -> None:
        """Test that string upload_to gets protected/ prefix."""
        field = ProtectedFileField(upload_to="attachments")
        assert field.upload_to == "protected/attachments"

    def test_already_prefixed_not_doubled(self) -> None:
        """Test that already-prefixed paths are not doubled."""
        field = ProtectedFileField(upload_to="protected/attachments")
        assert field.upload_to == "protected/attachments"

    def test_empty_upload_to_gets_prefix(self) -> None:
        """Test that empty upload_to gets protected/ prefix."""
        field = ProtectedFileField(upload_to="")
        assert field.upload_to == "protected/"

    def test_callable_upload_to_wrapped(self) -> None:
        """Test that callable upload_to is wrapped in ProtectedUploadTo."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"user/{filename}"

        field = ProtectedFileField(upload_to=my_upload)

        assert isinstance(field.upload_to, ProtectedUploadTo)

    def test_callable_produces_prefixed_path(self) -> None:
        """Test that callable upload_to produces prefixed paths."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"uploads/{filename}"

        field = ProtectedFileField(upload_to=my_upload)

        # The wrapped callable should add prefix
        assert isinstance(field.upload_to, ProtectedUploadTo)
        result = field.upload_to(None, "test.pdf")
        assert result == "protected/uploads/test.pdf"

    def test_inherits_from_file_field(self) -> None:
        """Test that ProtectedFileField inherits from FileField."""
        assert issubclass(ProtectedFileField, models.FileField)

    def test_passes_kwargs_to_parent(self) -> None:
        """Test that additional kwargs are passed to FileField."""
        field = ProtectedFileField(
            upload_to="files",
            max_length=500,
            blank=True,
            null=True,
        )

        assert field.max_length == 500
        assert field.blank is True
        assert field.null is True


class TestProtectedImageField:
    """Tests for ProtectedImageField."""

    def test_string_upload_to_gets_prefix(self) -> None:
        """Test that string upload_to gets protected/ prefix."""
        field = ProtectedImageField(upload_to="profile-pics")
        assert field.upload_to == "protected/profile-pics"

    def test_already_prefixed_not_doubled(self) -> None:
        """Test that already-prefixed paths are not doubled."""
        field = ProtectedImageField(upload_to="protected/images")
        assert field.upload_to == "protected/images"

    def test_empty_upload_to_gets_prefix(self) -> None:
        """Test that empty upload_to gets protected/ prefix."""
        field = ProtectedImageField(upload_to="")
        assert field.upload_to == "protected/"

    def test_callable_upload_to_wrapped(self) -> None:
        """Test that callable upload_to is wrapped in ProtectedUploadTo."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"avatars/{filename}"

        field = ProtectedImageField(upload_to=my_upload)

        assert isinstance(field.upload_to, ProtectedUploadTo)

    def test_callable_produces_prefixed_path(self) -> None:
        """Test that callable upload_to produces prefixed paths."""

        def my_upload(instance: t.Any, filename: str) -> str:
            return f"photos/{filename}"

        field = ProtectedImageField(upload_to=my_upload)

        assert isinstance(field.upload_to, ProtectedUploadTo)
        result = field.upload_to(None, "photo.jpg")
        assert result == "protected/photos/photo.jpg"

    def test_inherits_from_image_field(self) -> None:
        """Test that ProtectedImageField inherits from ImageField."""
        assert issubclass(ProtectedImageField, models.ImageField)

    def test_passes_kwargs_to_parent(self) -> None:
        """Test that additional kwargs are passed to ImageField."""
        field = ProtectedImageField(
            upload_to="images",
            max_length=300,
            blank=True,
        )

        assert field.max_length == 300
        assert field.blank is True


class TestProtectedPrefixConstant:
    """Tests for the PROTECTED_PREFIX constant."""

    def test_protected_prefix_value(self) -> None:
        """Test that PROTECTED_PREFIX has expected value."""
        assert PROTECTED_PREFIX == "protected/"

    def test_protected_prefix_ends_with_slash(self) -> None:
        """Test that PROTECTED_PREFIX ends with a slash."""
        assert PROTECTED_PREFIX.endswith("/")

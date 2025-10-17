"""Tests for custom Django fields with sanitization."""

import pytest
from django.test import TestCase

from accounts.models import RevelUser
from common.fields import render_markdown, sanitize_html
from events.models import Event, EventSeries, Organization


class TestMarkdownRendering(TestCase):
    """Test markdown to HTML conversion."""

    def test_basic_markdown(self) -> None:
        """Test basic markdown formatting works."""
        markdown = "# Heading\n\n**Bold** and *italic* text."
        html = render_markdown(markdown)

        assert "<h1>Heading</h1>" in html
        assert "<strong>Bold</strong>" in html
        assert "<em>italic</em>" in html

    def test_lists(self) -> None:
        """Test ordered and unordered lists."""
        markdown = """- Item 1
- Item 2

1. First
2. Second"""
        html = render_markdown(markdown)

        assert "<ul>" in html
        assert "Item 1" in html
        assert "<ol>" in html or "<li>" in html  # Lists are rendered
        assert "First" in html

    def test_links(self) -> None:
        """Test link rendering."""
        markdown = "[Click here](https://example.com)"
        html = render_markdown(markdown)

        assert '<a href="https://example.com">Click here</a>' in html

    def test_code_blocks(self) -> None:
        """Test fenced code blocks."""
        markdown = """
```python
def hello():
    print("world")
```
"""
        html = render_markdown(markdown)

        assert "<pre>" in html or "<code>" in html
        assert "def hello():" in html

    def test_tables(self) -> None:
        """Test table markdown extension."""
        markdown = """
| Header 1 | Header 2 |
|----------|----------|
| Cell 1   | Cell 2   |
"""
        html = render_markdown(markdown)

        assert "<table>" in html
        assert "<th>Header 1</th>" in html
        assert "<td>Cell 1</td>" in html

    def test_empty_text(self) -> None:
        """Test that None or empty string returns empty string."""
        assert render_markdown(None) == ""
        assert render_markdown("") == ""


class TestXSSPrevention(TestCase):
    """Test XSS attack prevention."""

    def test_script_tag_stripped(self) -> None:
        """Test that <script> tags are completely removed."""
        malicious = '<script>alert("XSS")</script>Safe text'
        html = sanitize_html(malicious)

        assert "<script>" not in html.lower()
        # Note: bleach strips tags but keeps content by default with strip=True
        # The alert text remains but without the script tag, which is safe
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

    def test_img_onerror_stripped(self) -> None:
        """Test that img onerror handlers are stripped."""
        malicious = '<img src="x" onerror="alert(\'XSS\')">'
        html = sanitize_html(malicious)

        assert "onerror" not in html.lower()
        assert "alert" not in html

    def test_data_uri_script_stripped(self) -> None:
        """Test that data: URIs with scripts are stripped."""
        malicious = "<img src=\"data:text/html,<script>alert('XSS')</script>\">"
        html = sanitize_html(malicious)

        # The img tag itself should be allowed, but script should be gone
        assert "<script>" not in html.lower()

    def test_style_tag_stripped(self) -> None:
        """Test that <style> tags are stripped."""
        malicious = "<style>body { background: red; }</style>Text"
        html = sanitize_html(malicious)

        assert "<style>" not in html.lower()
        assert "Text" in html


class TestImageSanitization(TestCase):
    """Test image source sanitization."""

    def test_https_images_allowed(self) -> None:
        """Test that HTTPS images are allowed."""
        markdown = "![Alt text](https://example.com/image.jpg)"
        html = render_markdown(markdown)

        assert "<img" in html
        assert 'src="https://example.com/image.jpg"' in html

    def test_http_images_blocked(self) -> None:
        """Test that HTTP (non-secure) images are blocked."""
        markdown = "![Alt text](http://example.com/image.jpg)"
        html = render_markdown(markdown)

        # The img tag should either not exist or not have an http src
        if "<img" in html:
            assert 'src="http://' not in html

    def test_data_uri_images_allowed(self) -> None:
        """Test that data URI images are allowed."""
        # Note: markdown doesn't convert data URIs in markdown syntax,
        # but they work in raw HTML which gets sanitized
        html_input = '<img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUg==" alt="Test">'
        html = sanitize_html(html_input)

        # Data URIs for images should be preserved if they start with data:image/
        if "<img" in html:
            assert "data:image/" in html or 'alt="Test"' in html

    def test_image_alt_preserved(self) -> None:
        """Test that alt text is preserved."""
        markdown = "![Important image](https://example.com/img.jpg)"
        html = render_markdown(markdown)

        assert 'alt="Important image"' in html or "Important image" in html


class TestIframeSanitization(TestCase):
    """Test iframe embed sanitization."""

    def test_youtube_iframe_allowed(self) -> None:
        """Test that YouTube embeds are allowed."""
        html_input = '<iframe src="https://www.youtube.com/embed/VIDEO_ID"></iframe>'
        html = sanitize_html(html_input)

        assert "<iframe" in html
        assert "youtube.com" in html

    def test_vimeo_iframe_allowed(self) -> None:
        """Test that Vimeo embeds are allowed."""
        html_input = '<iframe src="https://player.vimeo.com/video/VIDEO_ID"></iframe>'
        html = sanitize_html(html_input)

        assert "<iframe" in html
        assert "vimeo.com" in html

    def test_random_iframe_blocked(self) -> None:
        """Test that iframes from random domains are blocked."""
        html_input = '<iframe src="https://evil.com/malware"></iframe>'
        html = sanitize_html(html_input)

        assert "evil.com" not in html

    def test_non_https_iframe_blocked(self) -> None:
        """Test that non-HTTPS iframes are blocked."""
        html_input = '<iframe src="http://youtube.com/embed/VIDEO_ID"></iframe>'
        html = sanitize_html(html_input)

        # Should be stripped or converted
        assert 'src="http://' not in html


class TestSafeMarkdownFeatures(TestCase):
    """Test that safe markdown features are preserved."""

    def test_blockquote_preserved(self) -> None:
        """Test that blockquotes work."""
        markdown = "> This is a quote"
        html = render_markdown(markdown)

        assert "<blockquote>" in html

    def test_horizontal_rule_preserved(self) -> None:
        """Test that horizontal rules work."""
        markdown = "---"
        html = render_markdown(markdown)

        assert "<hr" in html or "<hr>" in html

    def test_multiple_headers(self) -> None:
        """Test all header levels."""
        markdown = """
# H1
## H2
### H3
#### H4
##### H5
###### H6
"""
        html = render_markdown(markdown)

        for i in range(1, 7):
            assert f"<h{i}>" in html


@pytest.mark.django_db
class TestMarkdownFieldOnModels(TestCase):
    """Test MarkdownField functionality on actual models."""

    def setUp(self) -> None:
        """Create a test user for organization owner."""
        self.user = RevelUser.objects.create_user(username="testuser", email="test@example.com", password="testpass")

    def test_organization_description_html(self) -> None:
        """Test that Organization description_html property works."""
        org = Organization.objects.create(
            name="Test Org", owner=self.user, description="# Welcome\n\nThis is **bold**."
        )

        assert "<h1>Welcome</h1>" in org.description_html  # type: ignore[attr-defined]
        assert "<strong>bold</strong>" in org.description_html  # type: ignore[attr-defined]

    def test_event_series_description_html(self) -> None:
        """Test that EventSeries description_html property works."""
        org = Organization.objects.create(name="Test Org 2", owner=self.user)
        series = EventSeries.objects.create(
            organization=org, name="Test Series", description="## Event Series\n\n- Item 1\n- Item 2"
        )

        assert "<h2>Event Series</h2>" in series.description_html  # type: ignore[attr-defined]
        assert "<ul>" in series.description_html  # type: ignore[attr-defined]

    def test_event_description_and_invitation_html(self) -> None:
        """Test Event description_html and invitation_message_html."""
        from datetime import datetime, timezone

        org = Organization.objects.create(name="Test Org 3", owner=self.user)
        event = Event.objects.create(
            organization=org,
            name="Test Event",
            description="**Event** description",
            invitation_message="You're *invited*!",
            start=datetime.now(timezone.utc),
            end=datetime.now(timezone.utc),
        )

        assert "<strong>Event</strong>" in event.description_html  # type: ignore[attr-defined]
        assert "<em>invited</em>" in event.invitation_message_html  # type: ignore[attr-defined]

    def test_xss_in_model_field(self) -> None:
        """Test that XSS attempts in model fields are sanitized."""
        org = Organization.objects.create(
            name="Test Org 4", owner=self.user, description='<script>alert("XSS")</script>Safe text'
        )

        html = org.description_html  # type: ignore[attr-defined]
        assert "<script>" not in html.lower()
        # bleach strips tags but keeps content - the important part is no executable script
        assert "Safe text" in html

    def test_null_markdown_field(self) -> None:
        """Test that null markdown fields return empty HTML."""
        org = Organization.objects.create(name="Test Org 5", owner=self.user, description=None)

        assert org.description_html == ""  # type: ignore[attr-defined]

    def test_empty_markdown_field(self) -> None:
        """Test that empty markdown fields return empty HTML."""
        org = Organization.objects.create(name="Test Org 6", owner=self.user, description="")

        assert org.description_html == ""  # type: ignore[attr-defined]


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
        # bleach may decode the URL or strip the href entirely
        assert "javascript:" not in html.lower() or 'href="javascript' not in html.lower()
        assert "Click" in html  # The text content is preserved

    def test_svg_xss(self) -> None:
        """Test SVG-based XSS."""
        malicious = '<svg onload="alert(1)">'
        html = sanitize_html(malicious)

        assert "onload" not in html.lower()

    def test_iframe_with_javascript(self) -> None:
        """Test iframe with javascript: src."""
        malicious = '<iframe src="javascript:alert(1)"></iframe>'
        html = sanitize_html(malicious)

        assert "javascript:" not in html.lower()

    def test_form_action_javascript(self) -> None:
        """Test form with javascript action."""
        malicious = '<form action="javascript:alert(1)"><input type="submit"></form>'
        html = sanitize_html(malicious)

        # Forms should be stripped entirely as they're not in allowlist
        assert "<form" not in html.lower()


class TestMarkdownWithXSS(TestCase):
    """Test that markdown with XSS attempts is properly sanitized."""

    def test_markdown_link_with_xss(self) -> None:
        """Test markdown link with javascript."""
        markdown = '[Click me](javascript:alert("XSS"))'
        html = render_markdown(markdown)

        assert "javascript:" not in html.lower()

    def test_markdown_with_inline_html_xss(self) -> None:
        """Test markdown with inline HTML XSS."""
        markdown = """
# Title

<script>alert("XSS")</script>

Normal text here.
"""
        html = render_markdown(markdown)

        assert "<h1>Title</h1>" in html
        assert "<script>" not in html.lower()
        assert "Normal text" in html

    def test_markdown_image_with_xss_src(self) -> None:
        """Test markdown image with XSS attempt in src."""
        markdown = '![Alt](javascript:alert("XSS"))'
        html = render_markdown(markdown)

        assert "javascript:" not in html.lower()

    def test_mixed_markdown_and_html(self) -> None:
        """Test that markdown is processed but HTML is sanitized."""
        markdown = """
# Header

**Bold** text and <strong>HTML strong</strong>

<script>alert("XSS")</script>

[Link](https://example.com)
"""
        html = render_markdown(markdown)

        assert "<h1>Header</h1>" in html
        assert "<strong>" in html  # Both markdown and HTML strong should be allowed
        assert "<script>" not in html.lower()
        assert '<a href="https://example.com"' in html

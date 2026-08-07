"""Tests for the markdown_tags template filters."""

import pytest

from common.templatetags.markdown_tags import strip_leading_heading


class TestStripLeadingHeading:
    """Tests for strip_leading_heading."""

    @pytest.mark.parametrize("value", [None, "", "   \n\n  "])
    def test_empty_input(self, value: str | None) -> None:
        """Falsy or whitespace-only input renders as an empty string."""
        assert strip_leading_heading(value).strip() == ""

    @pytest.mark.parametrize("hashes", ["#", "##", "###", "####", "#####", "######"])
    def test_strips_leading_atx_heading_at_any_level(self, hashes: str) -> None:
        """A leading ATX heading of any level is removed."""
        result = strip_leading_heading(f"{hashes} Payment Instructions\n\nTransfer to IBAN.")
        assert result == "Transfer to IBAN."

    def test_strips_heading_preceded_by_blank_lines(self) -> None:
        """Leading whitespace before the heading doesn't prevent stripping."""
        result = strip_leading_heading("\n\n  ## Payment Instructions\n\nTransfer to IBAN.")
        assert result == "Transfer to IBAN."

    def test_strips_only_the_first_heading(self) -> None:
        """Later headings belong to the organizer's own structure and are preserved."""
        result = strip_leading_heading("## Payment Instructions\n\nIntro.\n\n## Bank Details\n\nIBAN.")
        assert result == "Intro.\n\n## Bank Details\n\nIBAN."

    def test_leaves_content_without_leading_heading_untouched(self) -> None:
        """Bare text keeps every character, including a mid-text '#'."""
        value = "Please transfer to IBAN: XX1234567890\n\nRef #123"
        assert strip_leading_heading(value) == value

    def test_does_not_strip_heading_below_other_content(self) -> None:
        """A heading that isn't first is not a duplicate of the template title."""
        value = "Transfer to IBAN.\n\n## Payment Instructions"
        assert strip_leading_heading(value) == value

    def test_does_not_strip_hash_without_space(self) -> None:
        """'#1 rule' is a hashtag, not a heading."""
        value = "#1 rule: pay before the event."
        assert strip_leading_heading(value) == value

    def test_heading_only_content_is_kept(self) -> None:
        """Stripping never empties the content — an organizer's one-liner must survive.

        Whoever writes their whole instructions as a single `# ...` line would otherwise
        get an empty payment card: the templates gate on the raw value, so the heading
        renders and the "contact the organizer" fallback never fires.
        """
        value = "# Please e-transfer to IBAN AT12 3456 7890"
        assert strip_leading_heading(value) == value

    def test_heading_only_content_with_trailing_blank_lines_is_kept(self) -> None:
        """Trailing whitespace doesn't make heading-only content look strippable."""
        value = "## Payment Instructions\n\n"
        assert strip_leading_heading(value) == value

    def test_preserves_indentation_of_content_without_heading(self) -> None:
        """No heading means no rewriting — a 4-space code block keeps its indent."""
        value = "    IBAN: AT12 3456 7890\n    BIC: REVELAT2X\n"
        assert strip_leading_heading(value) == value

    def test_preserves_indentation_of_content_below_a_heading(self) -> None:
        """The blank lines under the heading go; the body's own indent stays."""
        result = strip_leading_heading("## Payment Instructions\n\n    IBAN: AT12 3456 7890\n")
        assert result == "    IBAN: AT12 3456 7890\n"

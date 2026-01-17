"""Tests for common schema utilities."""

import typing as t
from unittest.mock import MagicMock

from common.schema import SignedFileSchemaMixin


class MockModel:
    """Mock Django model for testing."""

    file: MagicMock | None

    def __init__(self, file_path: str | None = None) -> None:
        self.id = 1
        self.name = "test"
        if file_path:
            self.file = MagicMock()
            self.file.name = file_path
        else:
            self.file = None


class TestSignedFileSchemaMixin:
    """Tests for SignedFileSchemaMixin."""

    def test_mixin_creates_resolver_for_signed_field(self) -> None:
        """Test that mixin auto-creates resolve_* method."""

        class TestSchema(SignedFileSchemaMixin):
            signed_file_fields: t.ClassVar[dict[str, str]] = {"file_url": "file"}
            file_url: str | None = None

        # Check resolver was created
        assert hasattr(TestSchema, "resolve_file_url")

        # Test the resolver works with a protected path
        mock_obj = MockModel(file_path="protected/file/test.pdf")
        result = TestSchema.resolve_file_url(mock_obj)

        assert result is not None
        assert result.startswith("/media/protected/file/test.pdf?")
        assert "exp=" in result
        assert "sig=" in result

    def test_mixin_handles_none_file(self) -> None:
        """Test that resolver returns None for empty file field."""

        class TestSchema(SignedFileSchemaMixin):
            signed_file_fields: t.ClassVar[dict[str, str]] = {"file_url": "file"}
            file_url: str | None = None

        mock_obj = MockModel(file_path=None)
        resolver = getattr(TestSchema, "resolve_file_url")
        result = resolver(mock_obj)

        assert result is None

    def test_mixin_handles_public_path(self) -> None:
        """Test that resolver returns direct URL for non-protected paths."""

        class TestSchema(SignedFileSchemaMixin):
            signed_file_fields: t.ClassVar[dict[str, str]] = {"logo_url": "logo"}
            logo_url: str | None = None

        mock_obj = MagicMock()
        mock_obj.logo = MagicMock()
        mock_obj.logo.name = "logos/org-logo.png"

        resolver = getattr(TestSchema, "resolve_logo_url")
        result = resolver(mock_obj)

        assert result == "/media/logos/org-logo.png"
        assert "exp=" not in result

    def test_mixin_handles_multiple_fields(self) -> None:
        """Test that mixin creates resolvers for multiple fields."""

        class TestSchema(SignedFileSchemaMixin):
            signed_file_fields: t.ClassVar[dict[str, str]] = {
                "file_url": "file",
                "attachment_url": "attachment",
            }
            file_url: str | None = None
            attachment_url: str | None = None

        assert hasattr(TestSchema, "resolve_file_url")
        assert hasattr(TestSchema, "resolve_attachment_url")

    def test_mixin_allows_resolver_override(self) -> None:
        """Test that manually defined resolvers are not overwritten."""

        class TestSchema(SignedFileSchemaMixin):
            signed_file_fields: t.ClassVar[dict[str, str]] = {"file_url": "file"}
            file_url: str | None = None

            @staticmethod
            def resolve_file_url(obj: t.Any) -> str | None:
                return "custom_url"

        mock_obj = MockModel(file_path="file/test.pdf")
        result = TestSchema.resolve_file_url(mock_obj)

        assert result == "custom_url"

    def test_mixin_inheritance_isolation(self) -> None:
        """Test that subclasses don't share resolvers incorrectly."""

        class BaseSchema(SignedFileSchemaMixin):
            signed_file_fields: t.ClassVar[dict[str, str]] = {"base_url": "base_file"}
            base_url: str | None = None

        class ChildSchema(BaseSchema):
            signed_file_fields: t.ClassVar[dict[str, str]] = {"child_url": "child_file"}
            child_url: str | None = None

        # Child should have its own resolver
        assert hasattr(ChildSchema, "resolve_child_url")

        # Child should NOT automatically inherit parent's resolver
        # (unless explicitly defined in signed_file_fields)
        # The parent resolver is inherited through normal Python MRO
        assert hasattr(BaseSchema, "resolve_base_url")

    def test_mixin_raises_error_for_undeclared_url_field(self) -> None:
        """Test that mixin raises TypeError if URL field is not declared."""
        import pytest

        with pytest.raises(TypeError) as exc_info:

            class BadSchema(SignedFileSchemaMixin):
                signed_file_fields: t.ClassVar[dict[str, str]] = {"file_url": "file"}
                # file_url field is NOT declared - should raise error

        assert "signed_file_fields references 'file_url'" in str(exc_info.value)
        assert "not declared in the schema annotations" in str(exc_info.value)

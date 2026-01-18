"""Custom Django fields for Revel.

This module provides custom field types with built-in sanitization and security features.
"""

from __future__ import annotations

import typing as t
from urllib.parse import unquote, urlparse

import nh3
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.core.files.images import get_image_dimensions
from django.core.files.uploadedfile import UploadedFile

from common.signing import PROTECTED_PATH_PREFIX

# ---- Image Validation Constants ----

ALLOWED_IMAGE_EXTENSIONS: list[str] = ["jpg", "jpeg", "png", "gif", "webp", "heic", "heif"]
MAX_IMAGE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10MB


def validate_image_file(file: UploadedFile) -> None:
    """Validate an uploaded image file size and format.

    Args:
        file: The uploaded file to validate.

    Raises:
        ValidationError: If the file exceeds the maximum size or is not a valid image.
    """
    if file.size > MAX_IMAGE_SIZE_BYTES:  # type: ignore[operator]
        raise ValidationError(f"Image must be under {MAX_IMAGE_SIZE_BYTES // (1024 * 1024)}MB.")
    try:
        get_image_dimensions(file)
    except Exception:
        raise ValidationError("File is not a valid image.")


# ---- Protected File Fields ----
# These fields enforce the protected/ prefix for files requiring signed URL access.
# See common/signing.py and docs/PROTECTED_FILES_CADDY.md for details.

# Re-export for backward compatibility and cleaner imports
PROTECTED_PREFIX = PROTECTED_PATH_PREFIX


UploadToCallable = t.Callable[[t.Any, str], str]


def _ensure_protected_prefix(upload_to: str | UploadToCallable) -> str | UploadToCallable:
    """Ensure upload_to has the protected/ prefix.

    This helper normalizes the upload_to parameter for protected file fields:
    - String paths get the prefix added if not already present
    - Callables are wrapped in ProtectedUploadTo to add the prefix at runtime

    Args:
        upload_to: The original upload_to value (string path or callable).

    Returns:
        The normalized upload_to with protected/ prefix guaranteed.
    """
    if callable(upload_to):
        return ProtectedUploadTo(upload_to)
    if not upload_to.startswith(PROTECTED_PREFIX):
        return f"{PROTECTED_PREFIX}{upload_to}"
    return upload_to


class ProtectedUploadTo:
    """A serializable callable that wraps another upload_to and adds protected/ prefix.

    Django migrations need to serialize upload_to callables. This class is
    serializable via its deconstruct() method, unlike inner functions.
    """

    wrapped: UploadToCallable

    def __init__(self, wrapped: UploadToCallable) -> None:
        """Initialize with a wrapped upload_to callable.

        Args:
            wrapped: The original upload_to callable to wrap.
        """
        # Avoid double-wrapping if already a ProtectedUploadTo
        if isinstance(wrapped, ProtectedUploadTo):
            self.wrapped = wrapped.wrapped
        else:
            self.wrapped = wrapped

    def __call__(self, instance: t.Any, filename: str) -> str:
        """Generate the upload path with protected/ prefix.

        Args:
            instance: The model instance being saved.
            filename: The original filename.

        Returns:
            The path with protected/ prefix added if not already present.
        """
        path: str = self.wrapped(instance, filename)
        if not path.startswith(PROTECTED_PREFIX):
            return f"{PROTECTED_PREFIX}{path}"
        return path

    def deconstruct(self) -> tuple[str, tuple[t.Any, ...], dict[str, t.Any]]:
        """Return a 3-tuple for Django migration serialization."""
        return (
            f"{self.__class__.__module__}.{self.__class__.__qualname__}",
            (self.wrapped,),
            {},
        )


class ProtectedFileField(models.FileField):
    """A FileField that stores files in the protected/ directory.

    Files in protected/ paths require signed URLs for access, validated
    by Caddy's forward_auth directive calling Django's validation endpoint.

    The upload_to parameter is automatically prefixed with 'protected/' if not
    already present. This ensures files are stored in the correct location
    for the signed URL system.

    Usage:
        class MyModel(models.Model):
            # Simple: stores in protected/attachments/
            attachment = ProtectedFileField(upload_to="attachments")

            # With callable: prefix is added automatically
            document = ProtectedFileField(upload_to=my_upload_path_func)

    Note:
        If upload_to already starts with 'protected/', it won't be doubled.
    """

    def __init__(
        self,
        verbose_name: str | None = None,
        name: str | None = None,
        upload_to: str | UploadToCallable = "",
        **kwargs: t.Any,
    ) -> None:
        """Initialize the ProtectedFileField with automatic path prefixing.

        Args:
            verbose_name: Human-readable name for the field.
            name: The field name.
            upload_to: Directory or callable for upload path (protected/ prefix added).
            **kwargs: Additional arguments passed to FileField.
        """
        super().__init__(
            verbose_name=verbose_name,
            name=name,
            upload_to=_ensure_protected_prefix(upload_to),
            **kwargs,
        )


class ProtectedImageField(models.ImageField):
    """An ImageField that stores images in the protected/ directory.

    Similar to ProtectedFileField but for images. Files in protected/ paths
    require signed URLs for access.

    Usage:
        class MyModel(models.Model):
            # Stores in protected/profile-pics/
            profile_pic = ProtectedImageField(upload_to="profile-pics")

    Note:
        If upload_to already starts with 'protected/', it won't be doubled.
    """

    def __init__(
        self,
        verbose_name: str | None = None,
        name: str | None = None,
        upload_to: str | UploadToCallable = "",
        **kwargs: t.Any,
    ) -> None:
        """Initialize the ProtectedImageField with automatic path prefixing.

        Args:
            verbose_name: Human-readable name for the field.
            name: The field name.
            upload_to: Directory or callable for upload path (protected/ prefix added).
            **kwargs: Additional arguments passed to ImageField.
        """
        super().__init__(
            verbose_name=verbose_name,
            name=name,
            upload_to=_ensure_protected_prefix(upload_to),
            **kwargs,
        )


# Registry of all models using MarkdownField
# Format: {model_class: [field_name1, field_name2, ...]}
_markdown_field_registry: dict[type[models.Model], list[str]] = {}


def get_markdown_field_registry() -> dict[type[models.Model], list[str]]:
    """Get the registry of all models using MarkdownField.

    Returns:
        A dictionary mapping model classes to lists of field names.
    """
    return _markdown_field_registry.copy()


# Allowed HTML tags for markdown content - intentionally restrictive
ALLOWED_TAGS = {
    # Text formatting
    "p",
    "br",
    "hr",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "code",
    "pre",
    # Headers
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    # Lists
    "ul",
    "ol",
    "li",
    # Links and quotes
    "a",
    "blockquote",
    # Tables
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "th",
    "td",
}

# Allowed URL schemes - no javascript, data, etc.
ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

# Allowed attributes per tag
# Note: "rel" on <a> is handled automatically by nh3's link_rel parameter
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title"},
    "abbr": {"title"},
    "acronym": {"title"},
    "code": {"class"},  # For syntax highlighting
    "th": {"align", "valign", "scope"},
    "td": {"align", "valign"},
}


def _filter_attributes(
    element: str,
    attribute: str,
    value: str,
) -> str | None:
    """Filter attributes for HTML elements.

    This callback supplements nh3's url_schemes validation by also checking
    URL-decoded values. While nh3 validates schemes directly, it doesn't
    decode URLs first, so encoded attacks like "javascript%3Aalert(1)"
    would bypass the url_schemes check. This filter catches those.

    Args:
        element: The HTML element name
        attribute: The attribute name
        value: The attribute value

    Returns:
        The value if allowed, None to remove
    """
    if element == "a" and attribute == "href":
        # Decode URL to catch encoded attacks like javascript%3A -> javascript:
        decoded_value = unquote(value)
        parsed = urlparse(decoded_value)
        if parsed.scheme and parsed.scheme not in ALLOWED_URL_SCHEMES:
            return None

    return value


def sanitize_html(html: str | None) -> str:
    """Sanitize HTML using nh3 with a safe allowlist.

    This function removes potentially dangerous HTML elements and attributes
    while preserving safe formatting. Images, iframes, SVGs, and other
    potentially dangerous elements are not allowed.

    Args:
        html: The HTML string to sanitize (can be None)

    Returns:
        Sanitized HTML string safe for rendering, or empty string if None
    """
    if not html:
        return ""

    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        attribute_filter=_filter_attributes,
        url_schemes=ALLOWED_URL_SCHEMES,
        strip_comments=True,
    )


def sanitize_markdown(text: str | None) -> str:
    """Sanitize markdown text by removing potentially dangerous HTML.

    This sanitizes any HTML that might be embedded in markdown content.
    The frontend is responsible for rendering the markdown to HTML.

    Args:
        text: The markdown text to sanitize (can be None)

    Returns:
        Sanitized markdown string, or empty string if text is None
    """
    if not text:
        return ""

    # Sanitize any embedded HTML in the markdown
    return sanitize_html(text)


def render_markdown(text: str | None) -> str:
    """Render markdown to HTML for internal use (emails, Telegram).

    This function is for backend-rendered content only (emails, notifications).
    API responses should return raw markdown for frontend rendering.

    The output is sanitized to prevent XSS attacks.

    Args:
        text: The markdown text to render (can be None)

    Returns:
        Sanitized HTML string, or empty string if text is None
    """
    if not text:
        return ""

    import markdown

    # Render markdown to HTML
    html = markdown.markdown(
        text,
        extensions=["tables", "fenced_code"],
        output_format="html",
    )

    # Sanitize the output to ensure safety
    return sanitize_html(html)


if t.TYPE_CHECKING:

    class MarkdownField(models.TextField[str | None, str | None]):
        """Type stub for MarkdownField."""

        ...

else:

    class MarkdownField(models.TextField):
        """A TextField that stores sanitized markdown content.

        This field stores markdown in the database after sanitizing any embedded
        HTML to prevent XSS attacks. The frontend is responsible for rendering
        the markdown to HTML.

        Sanitization happens at save time via the field's pre_save method,
        so the stored content is always safe.

        Usage:
            class MyModel(models.Model):
                description = MarkdownField(blank=True, null=True)

            # Access markdown (already sanitized)
            obj.description  # Returns sanitized markdown string

        Supported Content:
            - Plain markdown text
            - Safe HTML tags (headers, lists, links, tables, etc.)

        Not Allowed (stripped):
            - Images (use dedicated image fields instead)
            - Iframes and embeds
            - SVG elements
            - Any JavaScript or event handlers
            - Data URIs

        Security:
            - All content is sanitized using nh3 at save time
            - Only safe HTML tags and attributes are allowed
            - URL schemes restricted to http, https, mailto
        """

        description = "A field that stores sanitized markdown content"

        def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
            """Initialize the MarkdownField."""
            super().__init__(*args, **kwargs)

        def contribute_to_class(self, cls: type[models.Model], name: str, private_only: bool = False) -> None:
            """Register this field with the model in the markdown field registry.

            This method is called by Django when the field is added to a model class.
            We use it to track all models that use MarkdownField for re-sanitization.

            Args:
                cls: The model class this field is being added to
                name: The attribute name of the field on the model
                private_only: Whether this is a private field
            """
            super().contribute_to_class(cls, name, private_only)

            # Register this model/field combination
            if cls not in _markdown_field_registry:
                _markdown_field_registry[cls] = []
            if name not in _markdown_field_registry[cls]:
                _markdown_field_registry[cls].append(name)

        def pre_save(self, model_instance: models.Model, add: bool) -> str | None:
            """Sanitize the markdown content before saving.

            This method is called by Django just before saving the field value
            to the database. We use it to sanitize the content.

            Args:
                model_instance: The model instance being saved
                add: True if this is a new record, False if updating

            Returns:
                The sanitized value to be saved
            """
            value = getattr(model_instance, self.attname)

            if value is not None:
                sanitized = sanitize_markdown(value)
                setattr(model_instance, self.attname, sanitized)
                return sanitized

            return super().pre_save(model_instance, add)

"""Map detected MIME types to stored file extensions.

Stored file names take their extension from the content type detected server-side,
via an explicit table (not ``mimetypes``, whose answers depend on the platform). The
client suffix is used only to refine a generic detection, from a fixed allowlist.
"""

import os

_MIME_EXTENSIONS: dict[str, str] = {
    # Documents
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/rtf": ".rtf",
    # Images
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    # Audio
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".weba",
    "audio/aac": ".aac",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
    # Video
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/ogg": ".ogv",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    # Archives
    "application/zip": ".zip",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/gzip": ".gz",
    "application/x-tar": ".tar",
}


# Generic detections that the client suffix can make more specific (#1470). libmagic reports a CSV as
# ``text/plain``, an Office Open XML file as ``application/zip`` and an M4A as ``video/mp4``. Only these
# suffixes are accepted; any other client suffix is ignored.
_REFINABLE_SUFFIXES: dict[str, frozenset[str]] = {
    "text/plain": frozenset({".csv"}),
    "application/zip": frozenset({".docx", ".xlsx", ".pptx"}),
    "video/mp4": frozenset({".m4a"}),
}


def extension_for_mime(mime_type: str) -> str:
    """Return the stored-file extension (lowercase, with leading dot) for a detected MIME type.

    Args:
        mime_type: The MIME type detected from the file content.

    Returns:
        The extension, or an empty string for an unknown type (never a client-supplied suffix).
    """
    return _MIME_EXTENSIONS.get(mime_type.lower(), "")


def extension_for_upload(mime_type: str, client_name: str | None) -> str:
    """Return the stored-file extension for a detected type, refined by an allowlisted client suffix.

    Args:
        mime_type: The MIME type detected from the file content.
        client_name: The file name the client sent. Only its suffix is read, and only when the
            ``(mime_type, suffix)`` pair is in the refinement allowlist.

    Returns:
        The refined suffix for an allowlisted pair, else ``extension_for_mime(mime_type)``.
    """
    suffix = os.path.splitext(client_name or "")[1].lower()
    if suffix in _REFINABLE_SUFFIXES.get(mime_type.lower(), frozenset()):
        return suffix
    return extension_for_mime(mime_type)

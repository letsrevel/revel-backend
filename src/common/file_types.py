"""Map detected MIME types to stored file extensions.

Stored file names must never take their extension from the client: the extension
is derived from the content type detected server-side, via an explicit table (not
``mimetypes``, whose answers depend on the platform).
"""

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


def extension_for_mime(mime_type: str) -> str:
    """Return the stored-file extension (lowercase, with leading dot) for a detected MIME type.

    Args:
        mime_type: The MIME type detected from the file content.

    Returns:
        The extension, or an empty string for an unknown type (never a client-supplied suffix).
    """
    return _MIME_EXTENSIONS.get(mime_type.lower(), "")

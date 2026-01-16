"""Service layer for questionnaire file management."""

import hashlib

import magic
from django.db import IntegrityError, transaction
from ninja.files import UploadedFile

from accounts.models import RevelUser
from common.utils import create_file_audit_and_scan

from ..exceptions import DisallowedMimeTypeError, FileSizeExceededError
from ..models import QuestionnaireFile
from ..schema import ALLOWED_QUESTIONNAIRE_MIME_TYPES

# Global maximum file size for questionnaire uploads (10MB)
# This is a hard limit before any processing - individual questions may have lower limits.
MAX_UPLOAD_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def upload_questionnaire_file(user: RevelUser, file: UploadedFile) -> QuestionnaireFile:
    """Upload a file to user's questionnaire file library.

    Handles:
    - Global file size validation (before processing)
    - MIME type detection from file content (not trusting client headers)
    - MIME type validation against global allowlist
    - SHA-256 hash calculation for deduplication
    - Race condition handling via IntegrityError
    - File storage with UUID path
    - Audit logging
    - Malware scan scheduling

    Args:
        user: The user uploading the file.
        file: The uploaded file.

    Returns:
        QuestionnaireFile: The created or existing file record.

    Raises:
        FileSizeExceededError: If the file exceeds the global maximum size.
        DisallowedMimeTypeError: If the detected MIME type is not in the global allowlist.
    """
    # Validate global file size limit before processing
    if file.size and file.size > MAX_UPLOAD_FILE_SIZE:
        raise FileSizeExceededError(f"File exceeds maximum upload size of {MAX_UPLOAD_FILE_SIZE // (1024 * 1024)}MB.")

    # Read file content and calculate hash
    file_content = file.read()
    file_hash = hashlib.sha256(file_content).hexdigest()
    file.seek(0)  # Reset file position for saving

    # Detect actual MIME type from file content (not trusting client header)
    detected_mime_type = magic.from_buffer(file_content, mime=True)

    # Validate MIME type against global allowlist
    if detected_mime_type not in ALLOWED_QUESTIONNAIRE_MIME_TYPES:
        raise DisallowedMimeTypeError(
            f"File type '{detected_mime_type}' is not allowed. "
            f"Allowed types: documents, images, audio, video, and archives."
        )

    # Check for existing file with same hash (deduplication)
    existing = QuestionnaireFile.objects.filter(uploader=user, file_hash=file_hash).first()
    if existing:
        return existing

    # Use the detected MIME type (more secure than client-provided)
    mime_type = detected_mime_type
    original_filename = file.name or "unnamed"

    # Create the file record with atomic transaction for race condition safety
    try:
        questionnaire_file = _create_file_record(
            user=user,
            file=file,
            original_filename=original_filename,
            file_hash=file_hash,
            mime_type=mime_type,
            file_size=len(file_content),
        )
    except IntegrityError:
        # Race condition - file was created by another request
        existing = QuestionnaireFile.objects.filter(uploader=user, file_hash=file_hash).first()
        if existing:
            return existing
        raise

    # Create audit record and trigger malware scan using shared helper
    create_file_audit_and_scan(
        app="questionnaires",
        model="questionnairefile",
        instance_pk=questionnaire_file.pk,
        field="file",
        file_hash=file_hash,
        uploader_email=user.email,
    )

    return questionnaire_file


@transaction.atomic
def _create_file_record(
    *,
    user: RevelUser,
    file: UploadedFile,
    original_filename: str,
    file_hash: str,
    mime_type: str,
    file_size: int,
) -> QuestionnaireFile:
    """Create a QuestionnaireFile record atomically."""
    questionnaire_file = QuestionnaireFile(
        uploader=user,
        original_filename=original_filename,
        file_hash=file_hash,
        mime_type=mime_type,
        file_size=file_size,
    )
    questionnaire_file.file.save(original_filename, file, save=False)
    questionnaire_file.save()
    return questionnaire_file

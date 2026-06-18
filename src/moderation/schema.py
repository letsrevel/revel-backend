import uuid

from ninja import Schema
from pydantic import AwareDatetime

from moderation.models import ContentReport


class ReportCreateSchema(Schema):
    content_type: str  # "app_label.model"
    object_id: uuid.UUID
    reason: ContentReport.Reason
    details: str = ""


class ReportSchema(Schema):
    id: uuid.UUID
    content_type: str
    object_id: uuid.UUID
    reason: ContentReport.Reason
    status: ContentReport.Status
    source: ContentReport.Source
    created_at: AwareDatetime

    @staticmethod
    def resolve_content_type(obj: ContentReport) -> str:
        ct = obj.content_type
        return f"{ct.app_label}.{ct.model}"

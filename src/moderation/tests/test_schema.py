import uuid

from moderation.models import ContentReport
from moderation.schema import ReportCreateSchema


def test_report_create_schema_defaults() -> None:
    payload = ReportCreateSchema(
        content_type="accounts.fooditem", object_id=uuid.uuid4(),
        reason=ContentReport.Reason.OFFENSIVE,
    )
    assert payload.details == ""
    assert payload.reason == ContentReport.Reason.OFFENSIVE

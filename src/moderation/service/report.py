from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from accounts.models import RevelUser
from common.utils import get_or_create_with_race_protection
from moderation.models import ContentReport
from moderation.registry import resolve_reportable
from moderation.schema import ReportCreateSchema


def create_content_report(reporter: RevelUser, payload: ReportCreateSchema) -> ContentReport:
    """Create (or return the existing open) report for a reportable target. 404 if not reportable."""
    instance, snapshot = resolve_reportable(payload.content_type, payload.object_id)
    ct = ContentType.objects.get_for_model(type(instance))
    report, _created = get_or_create_with_race_protection(
        ContentReport,
        Q(content_type=ct, object_id=instance.pk, reporter=reporter, status=ContentReport.Status.OPEN),
        {
            "content_type": ct,
            "object_id": instance.pk,
            "reporter": reporter,
            "status": ContentReport.Status.OPEN,
            "reason": payload.reason,
            "details": payload.details,
            "content_snapshot": snapshot,
            "source": ContentReport.Source.USER_REPORT,
        },
    )
    return report

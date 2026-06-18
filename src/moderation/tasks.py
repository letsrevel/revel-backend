from celery import shared_task
from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from accounts.models import FoodItem
from common.utils import get_or_create_with_race_protection
from moderation.blocklist.screen import Verdict, screen
from moderation.models import ContentReport


@shared_task
def sweep_food_items_for_blocklist() -> dict[str, int]:
    """Re-screen existing food items; escalate offenders to the moderation queue. Never deletes."""
    ct = ContentType.objects.get_for_model(FoodItem)
    escalated = 0
    for food in FoodItem.objects.all().iterator():
        verdict, ratio = screen(food.name)
        if verdict is Verdict.ALLOW:
            continue
        _report, created = get_or_create_with_race_protection(
            ContentReport,
            Q(
                content_type=ct,
                object_id=food.pk,
                source=ContentReport.Source.BLOCKLIST,
                status=ContentReport.Status.OPEN,
            ),
            {
                "content_type": ct,
                "object_id": food.pk,
                "reporter": None,
                "source": ContentReport.Source.BLOCKLIST,
                "status": ContentReport.Status.OPEN,
                "reason": ContentReport.Reason.OFFENSIVE,
                "content_snapshot": food.name,
                "score": ratio,
            },
        )
        if created:
            escalated += 1
    return {"escalated": escalated}

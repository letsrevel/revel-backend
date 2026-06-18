"""Guard for screening food-item names against the blocklist."""

import typing as t

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from common.utils import get_or_create_with_race_protection
from moderation.blocklist.screen import Verdict, screen
from moderation.exceptions import FoodItemBlockedError
from moderation.models import ContentReport

if t.TYPE_CHECKING:
    from accounts.models import FoodItem


def screen_food_item_name(
    name: str,
    *,
    food_item: "FoodItem | None" = None,
) -> None:
    """Screen a food-item name against the blocklist.

    BLOCK → raises FoodItemBlockedError (422).
    ESCALATE → files a blocklist ContentReport (reporter=None, system signal) if food_item is provided.
    ALLOW → no-op.
    """
    verdict, ratio = screen(name)
    if verdict is Verdict.BLOCK:
        raise FoodItemBlockedError
    if verdict is Verdict.ESCALATE and food_item is not None:
        ct = ContentType.objects.get_for_model(type(food_item))
        get_or_create_with_race_protection(
            ContentReport,
            Q(
                content_type=ct,
                object_id=food_item.pk,
                source=ContentReport.Source.BLOCKLIST,
                status=ContentReport.Status.OPEN,
            ),
            {
                "content_type": ct,
                "object_id": food_item.pk,
                "reporter": None,
                "source": ContentReport.Source.BLOCKLIST,
                "status": ContentReport.Status.OPEN,
                "reason": ContentReport.Reason.OFFENSIVE,
                "content_snapshot": food_item.name,
                "score": ratio,
            },
        )

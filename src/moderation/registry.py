"""Allow-list of reportable models. The generic report endpoint accepts only these,
preventing IDOR/enumeration against arbitrary models. Value = attribute carrying the
human-readable snapshot for the triage queue."""

import typing as t
import uuid

from django.apps import apps
from django.http import Http404

# "app_label.model" -> snapshot attribute path
REPORTABLE_MODELS: dict[str, str] = {
    "accounts.fooditem": "name",
    "accounts.dietaryrestriction": "notes",
    "accounts.userdietarypreference": "comment",
    "events.event": "name",
    "events.organization": "name",
    "accounts.reveluser": "bio",
}


def resolve_reportable(model_label: str, object_id: uuid.UUID) -> tuple[t.Any, str]:
    """Resolve a reportable target to (instance, snapshot). 404 if not reportable/missing."""
    snapshot_attr = REPORTABLE_MODELS.get(model_label.lower())
    if snapshot_attr is None:
        raise Http404("Content type is not reportable.")
    try:
        app_label, model_name = model_label.lower().split(".", 1)
        model = apps.get_model(app_label, model_name)
    except (ValueError, LookupError):
        raise Http404("Unknown content type.")
    instance = model.objects.filter(pk=object_id).first()
    if instance is None:
        raise Http404("Reported object does not exist.")
    snapshot = str(getattr(instance, snapshot_attr, "") or "")
    return instance, snapshot

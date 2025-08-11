import typing as t

from django.db import models, transaction
from pydantic import BaseModel

T = t.TypeVar("T", bound=models.Model)


@transaction.atomic
def update_db_instance(
    instance: T,
    payload: BaseModel | None = None,
    *,
    exclude_unset: bool = True,
    exclude_defaults: bool = True,
    **kwargs: t.Any,
) -> T:
    """Updates a DB instance given a Pydantic payload, safely within a select_for_update lock."""
    instance = instance.__class__.objects.select_for_update().get(pk=instance.pk)  # type: ignore[attr-defined]
    data = payload.model_dump(exclude_unset=exclude_unset, exclude_defaults=exclude_defaults) if payload else {}
    data.update(**kwargs)
    for key, value in data.items():
        setattr(instance, key, value)
    instance.save()
    return instance

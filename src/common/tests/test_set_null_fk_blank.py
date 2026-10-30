"""A nullable SET_NULL foreign key must also be blank=True on models that full_clean() on save.

``TimeStampedModel.save`` runs ``full_clean()``. ``blank`` decides whether validation accepts an empty
value, so once the referenced row is deleted (a user's GDPR deletion, a removed event or organization)
and the column is set to NULL, every later save of the referencing row would raise ``ValidationError``.
"""

import inspect

from django.apps import apps
from django.db import models


def _saves_with_full_clean(model: type[models.Model]) -> bool:
    for klass in model.__mro__:
        save = vars(klass).get("save")
        if save is None:
            continue
        try:
            if "full_clean" in inspect.getsource(save):
                return True
        except OSError, TypeError:
            continue
    return False


def test_set_null_foreign_keys_are_blank_on_full_clean_models() -> None:
    offenders = [
        f"{model._meta.label}.{field.name}"
        for model in apps.get_models()
        if _saves_with_full_clean(model)
        for field in model._meta.get_fields()
        if isinstance(field, models.ForeignKey | models.OneToOneField)
        and field.null
        and not field.blank
        and field.remote_field.on_delete is models.SET_NULL
    ]
    assert offenders == []

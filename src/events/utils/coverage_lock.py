"""The row lock that serializes a series pass coverage check with a new tier link (#1618)."""

import typing as t

from django.db import models, transaction


def lock_row(model: type[models.Model], pk: t.Any) -> None:
    """Lock one row ``FOR NO KEY UPDATE`` until the transaction ends.

    A coverage check reads the links, and ``series_pass_service.add_tier_links`` reads the rule. Both take this lock
    on the same row first, so the second one waits and then reads the committed state of the first. ``NO KEY`` does
    not conflict with the ``KEY SHARE`` that a deferred foreign key insert takes at commit.

    Outside an atomic block there is no transaction to hold the lock (and ``select_for_update`` raises), so the
    call does nothing.
    """
    lock_rows(model, [pk])


def lock_rows(model: type[models.Model], pks: t.Collection[t.Any]) -> None:
    """Lock several rows of one model as ``lock_row`` does, in pk order (the global lock order)."""
    if pks and transaction.get_connection().in_atomic_block:
        rows = model._base_manager.select_for_update(no_key=True).filter(pk__in=pks).order_by("pk")
        list(rows.values_list("pk", flat=True))

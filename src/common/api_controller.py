"""The project's ``api_controller``: ninja_extra's decorator with stable OpenAPI operationIds (#1551).

ninja_extra builds an operationId as ``<controller>_<function>`` and, with ``use_unique_op_id`` on (its default),
adds ``uuid4().hex[:8]``. So every OpenAPI dump renamed each operation, and every frontend regeneration renamed the
generated types. This decorator turns the suffix off by default. ``dump_openapi`` refuses a duplicate operationId,
so a collision gets an explicit ``operation_id=`` on its route.

Import it from here, not from ``ninja_extra``: a ruff rule (TID251) refuses ``ninja_extra.api_controller``.
"""

import typing as t

from ninja_extra import api_controller as _ninja_extra_api_controller  # noqa: TID251


def api_controller(prefix_or_class: t.Any = "", *args: t.Any, **kwargs: t.Any) -> t.Any:
    """``ninja_extra.api_controller`` with ``use_unique_op_id=False`` unless the caller sets it."""
    kwargs.setdefault("use_unique_op_id", False)
    return _ninja_extra_api_controller(prefix_or_class, *args, **kwargs)

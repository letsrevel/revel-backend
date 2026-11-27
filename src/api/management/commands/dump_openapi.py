"""Dump the OpenAPI schema in a json file."""

import contextlib
import json
import typing as t

from django.conf import settings
from django.core.management.base import BaseCommand
from ninja.openapi.schema import OpenAPISchema
from ninja.responses import NinjaJSONEncoder

from api.api import api


class OpenAPINameCollisionError(Exception):
    """Two different schema definitions competed for the same component name."""


@contextlib.contextmanager
def schema_name_collision_guard() -> t.Iterator[None]:
    """Fail schema generation when two DIFFERENT definitions share a component name.

    django-ninja's ``OpenAPISchema.add_schema_definitions`` is a bare
    ``dict.update`` (last writer wins), so two classes that share a bare name
    (e.g. two ``PaymentMethod`` enums) silently clobber each other in
    ``components.schemas`` — the spec stays valid, but every generated client
    narrows one of the types incorrectly (#782). Identical re-definitions
    (same name, same content — e.g. two enums with equal value sets) are fine
    and common, so only *conflicting* re-definitions raise.

    Scope: enums are compared whole; objects by their *property names*. Non-enum
    components legitimately differ for the SAME class between validation and
    serialization mode (ninja's known by_alias quirk — e.g. Decimal fields
    render ``str`` in responses but ``number|str`` in requests), so a
    whole-definition equality check would only produce false positives there.
    Those quirks change property *types*, never the property *set*, while two
    different classes sharing a name (oauth vs integrations ``ConnectionSchema``)
    almost always differ in it. Enum definitions are mode-invariant: a
    same-name/different-content enum is always a real clobber.
    """
    original = OpenAPISchema.add_schema_definitions

    def conflicts(existing: dict[str, t.Any], definition: dict[str, t.Any]) -> bool:
        if existing == definition:
            return False
        if "enum" in existing or "enum" in definition:
            return True
        return set(existing.get("properties", {})) != set(definition.get("properties", {}))

    def checked(self: OpenAPISchema, definitions: dict[str, t.Any]) -> None:
        for name, definition in definitions.items():
            existing = self.schemas.get(name)
            if existing is not None and conflicts(existing, definition):
                raise OpenAPINameCollisionError(
                    f"OpenAPI component name collision: {name!r} maps to two different definitions. "
                    f"Give one of the classes a distinct name (see issue #782)."
                )
        original(self, definitions)

    OpenAPISchema.add_schema_definitions = checked  # type: ignore[method-assign]
    try:
        yield
    finally:
        OpenAPISchema.add_schema_definitions = original  # type: ignore[method-assign]


class DuplicateOperationIdError(Exception):
    """Two operations share an operationId (#1551)."""


def assert_unique_operation_ids(schema: dict[str, t.Any]) -> None:
    """Refuse a schema in which two operations have the same operationId.

    The ids are stable (``common.api_controller``), so two routes with the same controller and function name
    collide. Ninja only logs a warning for that, and a generated client then drops one of the two operations.
    """
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            operation_id = operation.get("operationId") if isinstance(operation, dict) else None
            if operation_id is None:
                continue
            where = f"{method.upper()} {path}"
            if operation_id in seen:
                duplicates.append(f"{operation_id!r}: {seen[operation_id]} and {where}")
            else:
                seen[operation_id] = where
    if duplicates:
        raise DuplicateOperationIdError(
            "Duplicate OpenAPI operationIds; give one route of each pair an explicit operation_id=: "
            + "; ".join(duplicates)
        )


class Command(BaseCommand):
    help = "Dump the OpenAPI schema in a json file."

    def handle(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Dump the OpenAPI schema to a JSON file, refusing on component-name collisions."""
        output_file = settings.BASE_DIR.parent / ".artifacts" / "openapi.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with schema_name_collision_guard():
            schema = api.get_openapi_schema()
        assert_unique_operation_ids(schema)
        output_file.write_text(json.dumps(schema, indent=2, cls=NinjaJSONEncoder))
        self.stdout.write(self.style.SUCCESS(f"OpenAPI schema dumped to {output_file}"))

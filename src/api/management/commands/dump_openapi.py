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

    Scope: enum components only. Non-enum components legitimately differ for
    the SAME class between validation and serialization mode (ninja's known
    by_alias quirk — e.g. Decimal fields render ``str`` in responses but
    ``number|str`` in requests), so a whole-spec equality check would only
    produce false positives there. Enum definitions are mode-invariant: a
    same-name/different-content enum is always a real clobber.
    """
    original = OpenAPISchema.add_schema_definitions

    def checked(self: OpenAPISchema, definitions: dict[str, t.Any]) -> None:
        for name, definition in definitions.items():
            existing = self.schemas.get(name)
            if existing is not None and existing != definition and ("enum" in existing or "enum" in definition):
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


class Command(BaseCommand):
    help = "Dump the OpenAPI schema in a json file."

    def handle(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Dump the OpenAPI schema to a JSON file, refusing on component-name collisions."""
        output_file = settings.BASE_DIR.parent / ".artifacts" / "openapi.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with schema_name_collision_guard():
            schema = api.get_openapi_schema()
        output_file.write_text(json.dumps(schema, indent=2, cls=NinjaJSONEncoder))
        self.stdout.write(self.style.SUCCESS(f"OpenAPI schema dumped to {output_file}"))

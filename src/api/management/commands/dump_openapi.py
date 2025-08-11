"""Dump the OpenAPI schema in a json file."""

import json
import typing as t

from django.conf import settings
from django.core.management.base import BaseCommand
from ninja.responses import NinjaJSONEncoder

from api.api import api


class Command(BaseCommand):
    help = "Dump the OpenAPI schema in a json file."

    def handle(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Dump the OpenAPI schema to a JSON file."""
        output_file = settings.BASE_DIR.parent / ".artifacts" / "openapi.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(api.get_openapi_schema(), indent=2, cls=NinjaJSONEncoder))
        self.stdout.write(self.style.SUCCESS(f"OpenAPI schema dumped to {output_file}"))

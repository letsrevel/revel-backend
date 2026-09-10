"""OpenAPI schema builder with the API-wide default error responses (#826).

django-ninja builds each operation's ``responses`` solely from its own
``response=`` mapping, so statuses produced by shared machinery — the auth
class (401), permission classes and ``PermissionDenied`` (403), and ninja's
own request validation (422) — were declared on ~1% of routes although they
are reachable on nearly all of them. Declaring them per endpoint would drift
the moment a route is added; this builder adds them once, at spec time.

Schema-only by design: ``operation.response_models`` also drives runtime
serialisation of ``return status, obj`` tuples, so it is deliberately left
untouched.
"""

import typing as t
from http.client import responses as http_reasons

from ninja import Schema
from ninja.openapi.schema import OpenAPISchema
from ninja.operation import Operation
from ninja.types import DictStrAny

from common.schema import ErrorDetail, RequestValidationError


# ``OpenAPISchema._create_schema_from_model`` expects the single-field wrapper ninja
# builds around every ``response=`` model (it unwraps the ``response`` property), so
# the shared defaults are wrapped the same way, once.
class _ErrorDetailResponse(Schema):
    response: ErrorDetail


class _RequestValidationErrorResponse(Schema):
    response: RequestValidationError


class RevelOpenAPISchema(OpenAPISchema):
    """``OpenAPISchema`` that layers the universal error statuses onto every operation."""

    def responses(self, operation: Operation) -> dict[int, DictStrAny]:
        """Add 401/403 to secured operations and 422 to parameterised ones.

        An explicit per-endpoint 401/403 wins untouched. At 422 both shapes are
        real — a domain ``HttpError(422)`` renders ``{detail: str}`` while ninja's
        validation renders ``{detail: [...]}`` — so the validation schema is
        merged into an ``anyOf`` rather than replacing the declaration.
        """
        result = super().responses(operation)
        if operation.auth_callbacks:
            for status in (401, 403):
                result.setdefault(status, self._json_response(status, _ErrorDetailResponse))
        if operation.models:
            validation = self._json_response(422, _RequestValidationErrorResponse)
            declared = result.get(422)
            if declared is None:
                result[422] = validation
            else:
                self._merge_into_any_of(declared, validation)
        return result

    def _json_response(self, status: int, model: type[Schema]) -> DictStrAny:
        """Build a ``responses`` entry the way ninja does for an explicit ``response=`` model."""
        # ninja types the helper's param as a ``ParamModel`` but feeds it these
        # response wrappers itself (``Operation._create_response_model``).
        schema = self._create_schema_from_model(t.cast(t.Any, model), by_alias=True, mode="serialization")[0]
        return {
            "description": http_reasons[status],
            "content": {self.api.renderer.media_type: {"schema": schema}},
        }

    def _merge_into_any_of(self, declared: DictStrAny, extra: DictStrAny) -> None:
        """Union ``extra``'s schema into ``declared``'s, preserving an existing ``anyOf``."""
        media_type = self.api.renderer.media_type
        content = declared.setdefault("content", {}).setdefault(media_type, {})
        current = content.get("schema")
        extra_schema = extra["content"][media_type]["schema"]
        if current is None:  # declared as ``422: None``
            content["schema"] = extra_schema
            return
        options = current["anyOf"] if "anyOf" in current else [current]
        if extra_schema not in options:
            content["schema"] = {"anyOf": [*options, extra_schema]}

"""Unit tests for the events exception → HTTP status mapping.

The genuinely API-reachable mappings (organization-token guards, ticket/tier
guards) are exercised end-to-end in the controller tests. ``InvalidResourceStateError``
is raised inside ``AdditionalResource.clean()``, so ``Model.full_clean()`` re-wraps
it as a generic Django ``ValidationError`` and the global handler answers 400 over
HTTP (see ``test_resources_endpoints``). Its 422 mapping is kept for future-proofing
and asserted directly here.
"""

import json
import typing as t

from django.http import HttpRequest

from events.exception_handlers import HANDLERS
from events.exceptions import InvalidResourceStateError

# The handlers ignore the request, so a typed ``None`` keeps mypy happy.
_REQUEST = t.cast(HttpRequest, None)


def test_invalid_resource_state_error_maps_to_422() -> None:
    exc = InvalidResourceStateError({"link": ["Must not be set for resource type 'text'"]})
    response = HANDLERS[InvalidResourceStateError](_REQUEST, exc)
    assert response.status_code == 422
    assert json.loads(response.content) == {"detail": "link: Must not be set for resource type 'text'"}

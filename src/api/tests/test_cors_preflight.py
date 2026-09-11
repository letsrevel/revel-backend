"""CORS preflight must allow the custom token headers the controllers actually read."""

import pytest
from django.test import Client, override_settings

# The header names the token resolvers read (``HTTP_X_EVENT_TOKEN`` in
# events/controllers/event_public/base.py and ``HTTP_X_ORG_TOKEN`` in
# events/controllers/organization.py). If the CORS allowlist and these drift apart,
# a browser client sending the documented header is rejected at preflight.
TOKEN_HEADERS = ["x-event-token", "x-org-token"]


@pytest.mark.parametrize("header", TOKEN_HEADERS)
@override_settings(CORS_ALLOW_ALL_ORIGINS=True)
def test_preflight_allows_token_header(client: Client, header: str) -> None:
    response = client.options(
        "/api/organizations/",
        HTTP_ORIGIN="https://app.example.com",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS=header,
    )

    allowed = {h.strip().lower() for h in response["Access-Control-Allow-Headers"].split(",")}
    assert header in allowed

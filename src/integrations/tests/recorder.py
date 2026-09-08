"""Shared httpx recording transport for Eventbrite provider tests. No network."""

import typing as t

import httpx

from integrations.providers.eventbrite.provider import EventbriteProvider


class Recorder:
    """httpx transport that answers from a route table and records every request."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, dict[str, t.Any]]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        status, body = self.routes.get(key, (404, {"error": "NOT_FOUND", "error_description": "no route"}))
        return httpx.Response(status, json=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def provider(self) -> EventbriteProvider:
        return EventbriteProvider(client_id="K", client_secret="S", transport=self.transport())

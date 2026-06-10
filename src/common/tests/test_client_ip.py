"""Tests for the single source of truth for client IP resolution."""

from django.http import HttpRequest
from django.test import RequestFactory

from common.client_ip import get_client_ip


def test_trusts_x_real_ip() -> None:
    """X-Real-IP (set by Caddy from the Cloudflare-resolved IP) wins."""
    request: HttpRequest = RequestFactory().get("/", HTTP_X_REAL_IP="203.0.113.7", REMOTE_ADDR="10.0.0.1")
    assert get_client_ip(request) == "203.0.113.7"


def test_strips_whitespace() -> None:
    """Header values are stripped."""
    request: HttpRequest = RequestFactory().get("/", HTTP_X_REAL_IP=" 203.0.113.7 ")
    assert get_client_ip(request) == "203.0.113.7"


def test_ignores_x_forwarded_for() -> None:
    """X-Forwarded-For is client-controllable and must not be trusted."""
    request: HttpRequest = RequestFactory().get("/", HTTP_X_FORWARDED_FOR="6.6.6.6, 7.7.7.7", REMOTE_ADDR="10.0.0.1")
    assert get_client_ip(request) == "10.0.0.1"


def test_ignores_cf_connecting_ip() -> None:
    """CF-Connecting-IP is spoofable by direct-to-origin peers; Caddy resolves it for us."""
    request: HttpRequest = RequestFactory().get("/", HTTP_CF_CONNECTING_IP="6.6.6.6", REMOTE_ADDR="10.0.0.1")
    assert get_client_ip(request) == "10.0.0.1"


def test_falls_back_to_remote_addr() -> None:
    """Without X-Real-IP (dev, tests, internal calls), REMOTE_ADDR is used."""
    request: HttpRequest = RequestFactory().get("/", REMOTE_ADDR="192.0.2.1")
    assert get_client_ip(request) == "192.0.2.1"


def test_returns_empty_string_when_unknown() -> None:
    """No headers and no REMOTE_ADDR yields an empty string."""
    request: HttpRequest = RequestFactory().get("/", REMOTE_ADDR="")
    assert get_client_ip(request) == ""

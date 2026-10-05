"""Tests for the Cloudflare Turnstile verification service."""

import typing as t
from unittest.mock import MagicMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

from accounts.exceptions import TurnstileFailedError
from accounts.service.turnstile import SITEVERIFY_URL, is_turnstile_enabled, verify_turnstile


@pytest.fixture
def turnstile_on(settings: t.Any) -> t.Any:
    """Enable Turnstile with dummy keys."""
    settings.TURNSTILE_SITE_KEY = "site-key"
    settings.TURNSTILE_SECRET_KEY = "secret-key"
    return settings


def _response(status: int, payload: t.Any = None, *, text: str | None = None) -> httpx.Response:
    request = httpx.Request("POST", SITEVERIFY_URL)
    if text is not None:
        return httpx.Response(status, text=text, request=request)
    return httpx.Response(status, json=payload, request=request)


class TestIsTurnstileEnabled:
    def test_off_when_both_empty(self, settings: t.Any) -> None:
        settings.TURNSTILE_SITE_KEY = ""
        settings.TURNSTILE_SECRET_KEY = ""
        assert is_turnstile_enabled() is False

    @pytest.mark.parametrize(("site", "secret"), [("site-key", ""), ("", "secret-key")])
    def test_off_when_half_configured(self, settings: t.Any, site: str, secret: str) -> None:
        settings.TURNSTILE_SITE_KEY = site
        settings.TURNSTILE_SECRET_KEY = secret
        assert is_turnstile_enabled() is False

    def test_on_when_both_set(self, turnstile_on: t.Any) -> None:
        assert is_turnstile_enabled() is True


class TestVerifyTurnstile:
    @patch("accounts.service.turnstile.httpx.post")
    def test_disabled_skips_everything(self, mock_post: MagicMock, settings: t.Any) -> None:
        settings.TURNSTILE_SITE_KEY = ""
        settings.TURNSTILE_SECRET_KEY = ""
        verify_turnstile(None, "1.2.3.4")
        mock_post.assert_not_called()

    @pytest.mark.parametrize("token", [None, "", "   "])
    @patch("accounts.service.turnstile.httpx.post")
    def test_missing_token_rejected_without_calling_cloudflare(
        self, mock_post: MagicMock, turnstile_on: t.Any, token: str | None
    ) -> None:
        with pytest.raises(TurnstileFailedError):
            verify_turnstile(token, "1.2.3.4")
        mock_post.assert_not_called()

    @patch("accounts.service.turnstile.httpx.post")
    def test_success_passes_and_sends_secret_token_ip(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        mock_post.return_value = _response(200, {"success": True})
        verify_turnstile("tok", "1.2.3.4")
        mock_post.assert_called_once_with(
            SITEVERIFY_URL,
            data={"secret": "secret-key", "response": "tok", "remoteip": "1.2.3.4"},
            timeout=httpx.Timeout(5.0, connect=2.0),
        )

    @patch("accounts.service.turnstile.httpx.post")
    def test_empty_remote_ip_is_omitted(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        mock_post.return_value = _response(200, {"success": True})
        verify_turnstile("tok", "")
        assert "remoteip" not in mock_post.call_args.kwargs["data"]

    @patch("accounts.service.turnstile.httpx.post")
    def test_explicit_failure_raises(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        mock_post.return_value = _response(200, {"success": False, "error-codes": ["invalid-input-response"]})
        with pytest.raises(TurnstileFailedError):
            verify_turnstile("tok", "1.2.3.4")

    @patch("accounts.service.turnstile.httpx.post")
    def test_timeout_fails_open(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        mock_post.side_effect = httpx.ConnectTimeout("slow")
        verify_turnstile("tok", "1.2.3.4")  # no exception

    @patch("accounts.service.turnstile.httpx.post")
    def test_server_error_fails_open(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        mock_post.return_value = _response(503, {"success": False})
        verify_turnstile("tok", "1.2.3.4")  # no exception

    @patch("accounts.service.turnstile.httpx.post")
    def test_non_json_body_fails_open(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        mock_post.return_value = _response(200, text="<html>oops</html>")
        verify_turnstile("tok", "1.2.3.4")  # no exception

    @patch("accounts.service.turnstile.httpx.post")
    def test_client_error_status_rejected_not_bypassed(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        """A 4xx from siteverify must never let a registration through."""
        mock_post.return_value = _response(400, {"success": False})
        with capture_logs() as logs, pytest.raises(TurnstileFailedError):
            verify_turnstile("tok", "1.2.3.4")
        assert any(e["event"] == "turnstile_misconfigured" and e["log_level"] == "error" for e in logs)

    @pytest.mark.parametrize("code", ["invalid-input-secret", "missing-input-secret"])
    @patch("accounts.service.turnstile.httpx.post")
    def test_secret_error_is_logged_loudly(self, mock_post: MagicMock, turnstile_on: t.Any, code: str) -> None:
        """A wrong/missing secret is an operator problem: still rejected, but logged at error, not info."""
        mock_post.return_value = _response(200, {"success": False, "error-codes": [code]})
        with capture_logs() as logs, pytest.raises(TurnstileFailedError):
            verify_turnstile("tok", "1.2.3.4")
        assert any(e["event"] == "turnstile_misconfigured" and e["log_level"] == "error" for e in logs)

    @patch("accounts.service.turnstile.httpx.post")
    def test_visitor_rejection_logged_at_info(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        mock_post.return_value = _response(200, {"success": False, "error-codes": ["invalid-input-response"]})
        with capture_logs() as logs, pytest.raises(TurnstileFailedError):
            verify_turnstile("tok", "1.2.3.4")
        assert [e["log_level"] for e in logs if e["event"].startswith("turnstile_")] == ["info"]

    @patch("accounts.service.turnstile.httpx.post")
    def test_internal_error_code_fails_open(self, mock_post: MagicMock, turnstile_on: t.Any) -> None:
        """Cloudflare's own internal-error is an outage, not a verdict on the visitor."""
        mock_post.return_value = _response(200, {"success": False, "error-codes": ["internal-error"]})
        verify_turnstile("tok", "1.2.3.4")  # no exception

    @pytest.mark.parametrize("body", [[], "x", None])
    @patch("accounts.service.turnstile.httpx.post")
    def test_non_object_json_fails_open(self, mock_post: MagicMock, turnstile_on: t.Any, body: t.Any) -> None:
        mock_post.return_value = _response(200, body)
        verify_turnstile("tok", "1.2.3.4")  # no exception, no 500

    @pytest.mark.parametrize("codes", [None, "internal-error", 3])
    @patch("accounts.service.turnstile.httpx.post")
    def test_malformed_error_codes_fail_open(self, mock_post: MagicMock, turnstile_on: t.Any, codes: t.Any) -> None:
        """A non-list ``error-codes`` is a malformed response (outage path), never a 500."""
        mock_post.return_value = _response(200, {"success": False, "error-codes": codes})
        verify_turnstile("tok", "1.2.3.4")  # no exception

"""Tests for the Cloudflare Custom Hostnames client (#1151). The HTTP layer is mocked; no network."""

import typing as t
from unittest.mock import patch

import httpx
import pytest
from structlog.testing import capture_logs

from events.exceptions import CloudflareAPIError
from events.service import cloudflare_custom_hostnames as cloudflare

CF_RESULT: dict[str, t.Any] = {
    "id": "24c8c68e-bec2-49b6-868e-f06373780630",
    "hostname": "events.example.com",
    "status": "pending",
    "verification_errors": ["custom hostname does not CNAME to this zone."],
    "ownership_verification": {
        "type": "txt",
        "name": "_cf-custom-hostname.events.example.com",
        "value": "0e2d5a7f-1548-4f27-8c05-b577cb14f4ec",
    },
    "ownership_verification_http": {"http_url": "http://events.example.com/x", "http_body": "b"},
    "ssl": {
        "id": "ssl-1",
        "status": "pending_validation",
        "method": "http",
        "type": "dv",
        "validation_records": [
            {"status": "pending", "txt_name": "_acme-challenge.events.example.com", "txt_value": "v"}
        ],
        "validation_errors": [{"message": "SERVFAIL"}],
    },
    "created_at": "2022-10-06T19:35:33.143257Z",
}


@pytest.fixture(autouse=True)
def cloudflare_settings(settings: t.Any) -> None:
    settings.CLOUDFLARE_API_TOKEN = "tok"
    settings.CLOUDFLARE_ZONE_ID = "zone1"


def _response(status: int, body: t.Any, method: str = "GET") -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request(method, "https://api.cloudflare.com/x"))


def _ok(result: t.Any, status: int = 200) -> httpx.Response:
    return _response(status, {"success": True, "errors": [], "messages": [], "result": result})


@pytest.mark.parametrize(
    ("token", "zone", "expected"),
    [("tok", "zone1", True), ("", "zone1", False), ("tok", "", False), ("", "", False)],
)
def test_is_configured_needs_token_and_zone(settings: t.Any, token: str, zone: str, expected: bool) -> None:
    settings.CLOUDFLARE_API_TOKEN = token
    settings.CLOUDFLARE_ZONE_ID = zone
    assert cloudflare.is_configured() is expected


def test_create_posts_hostname_with_http_dv_and_parses_response() -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok(CF_RESULT, 201)) as request:
        hosted = cloudflare.create_custom_hostname("events.example.com")

    args, kwargs = request.call_args
    assert args == ("POST", "https://api.cloudflare.com/client/v4/zones/zone1/custom_hostnames")
    assert kwargs["json"] == {"hostname": "events.example.com", "ssl": {"method": "http", "type": "dv"}}
    assert kwargs["headers"] == {"Authorization": "Bearer tok"}
    assert kwargs["timeout"] == cloudflare.REQUEST_TIMEOUT_SECONDS
    assert hosted.id == CF_RESULT["id"]
    assert hosted.status == "pending"
    assert hosted.verification_errors == ["custom hostname does not CNAME to this zone."]
    assert hosted.ownership_verification is not None
    assert hosted.ownership_verification.name == "_cf-custom-hostname.events.example.com"
    assert hosted.ssl is not None
    assert hosted.ssl.status == "pending_validation"
    assert hosted.ssl.validation_records[0].txt_name == "_acme-challenge.events.example.com"
    assert hosted.ssl.validation_errors[0].message == "SERVFAIL"


def test_get_requests_the_hostname_by_id() -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok(CF_RESULT)) as request:
        hosted = cloudflare.get_custom_hostname("abc")

    assert request.call_args.args == ("GET", "https://api.cloudflare.com/client/v4/zones/zone1/custom_hostnames/abc")
    assert hosted.hostname == "events.example.com"


def test_get_tolerates_missing_optional_objects() -> None:
    with patch(
        "events.service.cloudflare_custom_hostnames.httpx.request",
        return_value=_ok({"id": "i", "hostname": "h.example.com", "status": "active"}),
    ):
        hosted = cloudflare.get_custom_hostname("i")

    assert hosted.ssl is None
    assert hosted.ownership_verification is None
    assert hosted.verification_errors == []


def test_delete_calls_delete() -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok({"id": "abc"})) as request:
        cloudflare.delete_custom_hostname("abc")

    assert request.call_args.args[0] == "DELETE"


def test_api_error_envelope_raises_with_status_and_codes() -> None:
    body = {"success": False, "errors": [{"code": 1406, "message": "Duplicate custom hostname found."}], "result": None}
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_response(409, body)):
        with pytest.raises(CloudflareAPIError) as exc_info:
            cloudflare.create_custom_hostname("events.example.com")

    assert exc_info.value.status_code == 409
    assert exc_info.value.error_codes == (1406,)
    assert "Duplicate custom hostname found." in str(exc_info.value)


def test_success_false_on_a_2xx_still_raises() -> None:
    with patch(
        "events.service.cloudflare_custom_hostnames.httpx.request",
        return_value=_response(200, {"success": False, "errors": []}),
    ):
        with pytest.raises(CloudflareAPIError):
            cloudflare.get_custom_hostname("abc")


def test_non_json_body_raises_with_status() -> None:
    bad = httpx.Response(502, text="<html>bad gateway</html>", request=httpx.Request("GET", "https://x"))
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=bad):
        with pytest.raises(CloudflareAPIError) as exc_info:
            cloudflare.get_custom_hostname("abc")

    assert exc_info.value.status_code == 502


def test_transport_error_raises_without_status() -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", side_effect=httpx.ConnectTimeout("slow")):
        with pytest.raises(CloudflareAPIError) as exc_info:
            cloudflare.get_custom_hostname("abc")

    assert exc_info.value.status_code is None


# ---- robustness: every failure is a CloudflareAPIError, logged once, without secrets ----


@pytest.mark.parametrize(
    "call", [lambda: cloudflare.create_custom_hostname("h.example.com"), lambda: cloudflare.get_custom_hostname("abc")]
)
def test_missing_result_raises_cloudflare_api_error(call: t.Callable[[], t.Any]) -> None:
    ok_without_result = _response(200, {"success": True, "errors": [], "result": None})
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=ok_without_result):
        with pytest.raises(CloudflareAPIError, match="no result"):
            call()


@pytest.mark.parametrize(
    "call", [lambda: cloudflare.create_custom_hostname("h.example.com"), lambda: cloudflare.get_custom_hostname("abc")]
)
def test_malformed_result_shape_raises_cloudflare_api_error(call: t.Callable[[], t.Any]) -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok({"id": "i"})):
        with pytest.raises(CloudflareAPIError, match="unexpected shape"):
            call()


def test_malformed_create_result_carries_the_raw_hostname_id() -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok({"id": "i"})):
        with pytest.raises(CloudflareAPIError) as exc_info:
            cloudflare.create_custom_hostname("h.example.com")

    assert exc_info.value.hostname_id == "i"


def test_malformed_create_result_without_a_string_id_carries_none() -> None:
    with patch("events.service.cloudflare_custom_hostnames.httpx.request", return_value=_ok({"id": 5})):
        with pytest.raises(CloudflareAPIError) as exc_info:
            cloudflare.create_custom_hostname("h.example.com")

    assert exc_info.value.hostname_id is None


def test_every_failure_cause_is_logged_exactly_once_without_secrets() -> None:
    non_json = httpx.Response(502, text="<html>", request=httpx.Request("GET", "https://x"))
    envelope_error = _response(403, {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]})
    cases: list[tuple[t.Any, str]] = [
        ({"side_effect": httpx.ConnectTimeout("slow")}, "transport"),
        ({"return_value": non_json}, "unparseable_body"),
        ({"return_value": envelope_error}, "error_response"),
        ({"return_value": _ok(None)}, "missing_result"),
        ({"return_value": _ok({"id": "i"})}, "bad_result_shape"),
    ]
    for patch_kwargs, cause in cases:
        with (
            patch("events.service.cloudflare_custom_hostnames.httpx.request", **patch_kwargs),
            capture_logs() as logs,
            pytest.raises(CloudflareAPIError),
        ):
            cloudflare.get_custom_hostname("abc")

        api_errors = [log for log in logs if log["event"] == "cloudflare_api_error"]
        assert [log["cause"] for log in api_errors] == [cause]
        assert "Bearer" not in str(api_errors)

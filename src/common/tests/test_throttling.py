"""Tests for the throttle classes in ``common.throttling``.

Regression coverage for #936: every throttle must own a distinct cache bucket
(``scope``), otherwise a long-window throttle such as 25/day is evaluated against
the request history of the 100/min default throttle.
"""

import inspect
import typing as t
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.test import RequestFactory
from ninja_extra.throttling import AnonRateThrottle, UserRateThrottle

from common import throttling
from common.throttling import (
    AnonDefaultThrottle,
    DisableableThrottleMixin,
    SendAnnouncementThrottle,
    UserDefaultThrottle,
)

USER_PK = "11111111-1111-1111-1111-111111111111"
CLIENT_IP = "203.0.113.7"


def _all_throttles() -> list[type[t.Any]]:
    return [
        cls
        for _, cls in inspect.getmembers(throttling, inspect.isclass)
        if issubclass(cls, DisableableThrottleMixin) and cls is not DisableableThrottleMixin
    ]


def _user_throttles() -> list[type[t.Any]]:
    return [cls for cls in _all_throttles() if issubclass(cls, UserRateThrottle) and cls is not UserDefaultThrottle]


def _anon_throttles() -> list[type[t.Any]]:
    return [cls for cls in _all_throttles() if issubclass(cls, AnonRateThrottle) and cls is not AnonDefaultThrottle]


@pytest.fixture(autouse=True)
def _enable_throttling(settings: t.Any) -> None:
    """The local ``.env`` may disable throttling; these tests need it live."""
    settings.DISABLE_THROTTLING = False


@pytest.fixture
def user_request() -> t.Any:
    request = RequestFactory().get("/")
    request.user = t.cast(t.Any, SimpleNamespace(is_authenticated=True, pk=USER_PK))
    return request


@pytest.fixture
def anon_request() -> t.Any:
    request = RequestFactory().get("/", REMOTE_ADDR=CLIENT_IP)
    request.user = t.cast(t.Any, SimpleNamespace(is_authenticated=False))
    return request


def _fill_bucket(key: str, count: int) -> None:
    """Simulate ``count`` requests made just now under ``key``."""
    import time

    now = time.time()
    cache.set(key, [now] * count, 60)


def test_every_throttle_has_a_unique_scope() -> None:
    scopes = [cls.scope for cls in _all_throttles()]
    assert None not in scopes
    assert len(scopes) == len(set(scopes)), f"duplicate throttle scopes: {sorted(scopes)}"


@pytest.mark.parametrize("throttle_cls", _user_throttles(), ids=lambda c: c.__name__)
def test_user_throttles_ignore_the_default_user_bucket(throttle_cls: type[t.Any], user_request: t.Any) -> None:
    """A user who exhausted the default 100/min bucket must not be refused by an unrelated throttle."""
    _fill_bucket(f"throttle_user_{USER_PK}", 100)

    throttle = throttle_cls()

    assert throttle.get_cache_key(user_request) != f"throttle_user_{USER_PK}"
    assert throttle.allow_request(user_request) is True


@pytest.mark.parametrize("throttle_cls", _anon_throttles(), ids=lambda c: c.__name__)
def test_anon_throttles_ignore_the_default_anon_bucket(throttle_cls: type[t.Any], anon_request: t.Any) -> None:
    _fill_bucket(f"throttle_anon_{CLIENT_IP}", 100)

    throttle = throttle_cls()

    assert throttle.get_cache_key(anon_request) != f"throttle_anon_{CLIENT_IP}"
    assert throttle.allow_request(anon_request) is True


def test_send_announcement_throttle_enforces_its_own_rate(user_request: t.Any) -> None:
    """25/day means the 26th send in a day is refused, independent of other traffic."""
    throttle = SendAnnouncementThrottle()

    for _ in range(25):
        assert throttle.allow_request(user_request) is True
        # Unrelated default-throttled traffic between sends must not matter.
        assert UserDefaultThrottle().allow_request(user_request) is True

    assert throttle.allow_request(user_request) is False


def test_disabled_throttling_short_circuits(settings: t.Any, user_request: t.Any) -> None:
    settings.DISABLE_THROTTLING = True
    throttle = SendAnnouncementThrottle()
    key = throttle.get_cache_key(user_request)
    assert key is not None
    _fill_bucket(key, 100)

    assert throttle.allow_request(user_request) is True

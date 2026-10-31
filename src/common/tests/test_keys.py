"""Tests for key-rotation helpers (``common.keys``), the system check and the source guard."""

import re
import typing as t
from datetime import timedelta
from pathlib import Path

import jwt
import pytest
from django.core.checks import Warning
from django.utils import timezone

from common import keys
from common.checks import UNSUBSCRIBE_KEYS_CHECK_ID, check_secret_key_rotation

ALG = "HS256"


def _token(key: str, **extra: t.Any) -> str:
    claims = {"exp": timezone.now() + timedelta(minutes=5), "aud": "x", **extra}
    return jwt.encode(claims, key, algorithm=ALG)


class TestKeyAccessors:
    def test_defaults(self, settings: t.Any) -> None:
        settings.SECRET_KEY = "new"
        settings.SECRET_KEY_FALLBACKS = ["old"]
        settings.UNSUBSCRIBE_SIGNING_KEYS = []
        assert keys.signing_key() == "new"
        assert keys.verification_keys() == ["new", "old"]
        assert keys.unsubscribe_signing_key() == "new"
        assert keys.unsubscribe_verification_keys() == ["new", "old"]

    def test_unsubscribe_keys_override(self, settings: t.Any) -> None:
        settings.SECRET_KEY = "new"
        settings.SECRET_KEY_FALLBACKS = []
        settings.UNSUBSCRIBE_SIGNING_KEYS = ["u1", "old"]
        assert keys.unsubscribe_signing_key() == "u1"
        assert keys.unsubscribe_verification_keys() == ["u1", "old"]
        assert keys.verification_keys() == ["new"]


class TestDecodeJwt:
    def test_fallback_key_accepted(self) -> None:
        token = _token("old")
        assert keys.decode_jwt(token, ["new", "old"], algorithms=[ALG], audience="x")["aud"] == "x"

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(jwt.InvalidSignatureError):
            keys.decode_jwt(_token("other"), ["new", "old"], algorithms=[ALG], audience="x")

    def test_expired_raised_immediately(self, monkeypatch: pytest.MonkeyPatch) -> None:
        token = _token("new", exp=timezone.now() - timedelta(minutes=1))
        calls: list[str] = []
        real = jwt.decode

        def spy(tok: str, key: str, **kw: t.Any) -> t.Any:
            calls.append(key)
            return real(tok, key, **kw)

        monkeypatch.setattr(jwt, "decode", spy)
        with pytest.raises(jwt.ExpiredSignatureError):
            keys.decode_jwt(token, ["new", "old"], algorithms=[ALG], audience="x")
        assert calls == ["new"]

    def test_wrong_audience_not_masked(self) -> None:
        with pytest.raises(jwt.InvalidAudienceError):
            keys.decode_jwt(_token("old"), ["new", "old"], algorithms=[ALG], audience="y")


class TestRotationCheck:
    def test_warns_when_rotating_without_unsubscribe_keys(self, settings: t.Any) -> None:
        settings.SECRET_KEY_FALLBACKS = ["old"]
        settings.UNSUBSCRIBE_SIGNING_KEYS = []
        msgs = check_secret_key_rotation(None)
        assert [m.id for m in msgs] == [UNSUBSCRIBE_KEYS_CHECK_ID]
        assert isinstance(msgs[0], Warning)

    def test_quiet_without_fallbacks(self, settings: t.Any) -> None:
        settings.SECRET_KEY_FALLBACKS = []
        settings.UNSUBSCRIBE_SIGNING_KEYS = []
        assert check_secret_key_rotation(None) == []

    def test_quiet_with_unsubscribe_keys(self, settings: t.Any) -> None:
        settings.SECRET_KEY_FALLBACKS = ["old"]
        settings.UNSUBSCRIBE_SIGNING_KEYS = ["u", "old"]
        assert check_secret_key_rotation(None) == []


def test_no_direct_secret_key_reads_in_source() -> None:
    """New code must sign/verify through ``common.keys`` so rotation can't be bypassed."""
    src = Path(__file__).resolve().parents[2]
    offenders = []
    for path in src.rglob("*.py"):
        parts = path.relative_to(src).parts
        rel = "/".join(parts)
        if (
            "tests" in parts
            or "migrations" in parts
            or rel.startswith("revel/settings/")
            or rel == "common/keys.py"
            or path.name == "conftest.py"
        ):
            continue
        if re.search(r"settings\.SECRET_KEY\b", path.read_text()):
            offenders.append(rel)
    assert offenders == []

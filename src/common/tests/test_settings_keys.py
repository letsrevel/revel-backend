"""Key-list env parsing must keep every character Django's secret-key alphabet can contain."""

import pytest

from revel.settings.base import split_keys

TRICKY = "abc#def$g'h\"i\\j%k*l"


def test_special_characters_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEYS", f"{TRICKY}, second#key ,")
    assert split_keys("TEST_KEYS") == [TRICKY, "second#key"]


def test_empty_and_blank_entries_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_KEYS", " , ,a,, ")
    assert split_keys("TEST_KEYS") == ["a"]
    monkeypatch.setenv("TEST_KEYS", "")
    assert split_keys("TEST_KEYS") == []


def test_unset_is_empty() -> None:
    assert split_keys("TEST_KEYS_NOT_SET") == []

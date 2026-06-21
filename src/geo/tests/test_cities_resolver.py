import typing as t
from pathlib import Path

import pytest

from geo import conf


def test_resolver_prefers_full_when_present(tmp_path: Path, monkeypatch: t.Any) -> None:
    full = tmp_path / "worldcities.csv"
    mini = tmp_path / "worldcities.mini.csv"
    full.write_text("city\n", encoding="utf-8")
    mini.write_text("city\n", encoding="utf-8")
    monkeypatch.setattr(conf, "WORLDCITIES_CSV_PATH", full)
    monkeypatch.setattr(conf, "WORLDCITIES_MINI_CSV_PATH", mini)
    assert conf.resolve_worldcities_csv() == full


def test_resolver_falls_back_to_mini_when_full_absent(tmp_path: Path, monkeypatch: t.Any) -> None:
    full = tmp_path / "worldcities.csv"  # not created
    mini = tmp_path / "worldcities.mini.csv"
    mini.write_text("city\n", encoding="utf-8")
    monkeypatch.setattr(conf, "WORLDCITIES_CSV_PATH", full)
    monkeypatch.setattr(conf, "WORLDCITIES_MINI_CSV_PATH", mini)
    assert conf.resolve_worldcities_csv() == mini


@pytest.mark.django_db
def test_resolver_returns_existing_file_in_checkout(monkeypatch: t.Any) -> None:
    """The mini CSV is tracked, so the resolver always returns an existing file."""
    assert conf.resolve_worldcities_csv().exists()

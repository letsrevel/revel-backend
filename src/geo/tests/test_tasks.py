"""Tests for the IP2Location database download task."""

import io
import typing as t
import zipfile
from pathlib import Path

import pytest
import requests
from pytest import MonkeyPatch

from geo import tasks

FAKE_BIN_CONTENT = b"fake-ip2location-binary-database-content"


class FakeResponse:
    """Minimal stand-in for a streaming requests.Response."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> t.Iterator[bytes]:
        stream = io.BytesIO(self.payload)
        while chunk := stream.read(chunk_size):
            yield chunk


class FakeRecord:
    country_short = "US"


class FakeIP2Location:
    """Validation stub: pretends the extracted .BIN is a healthy database."""

    record: t.ClassVar[FakeRecord | None] = FakeRecord()

    def __init__(self, path: str) -> None:
        self.path = path

    def get_all(self, ip: str) -> FakeRecord | None:
        return self.record


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    """Point the task at a temporary database path with a pre-existing live DB."""
    path = tmp_path / "IP2LOCATION-LITE-DB5.BIN"
    path.write_bytes(b"previous-live-database")
    monkeypatch.setattr(tasks, "IP2LOCATION_DB_PATH", path)
    monkeypatch.setattr(tasks, "IP2Location", FakeIP2Location)
    monkeypatch.setattr(FakeIP2Location, "record", FakeRecord())
    return path


def _mock_download(monkeypatch: MonkeyPatch, payload: bytes) -> None:
    monkeypatch.setattr(requests, "get", lambda *args, **kwargs: FakeResponse(payload))


def test_download_extracts_bin_from_zip(db_path: Path, monkeypatch: MonkeyPatch) -> None:
    """The .BIN member is extracted from the archive and replaces the live file."""
    payload = make_zip(
        {
            "LICENSE_LITE.TXT": b"license",
            "README_LITE.TXT": b"readme",
            "IP2LOCATION-LITE-DB5.IPV6.BIN": FAKE_BIN_CONTENT,
        }
    )
    _mock_download(monkeypatch, payload)

    tasks.download_ip2location()

    assert db_path.read_bytes() == FAKE_BIN_CONTENT
    assert not db_path.with_suffix(".zip.tmp").exists()
    assert not db_path.with_suffix(".bin.tmp").exists()


def test_download_rejects_archive_without_bin(db_path: Path, monkeypatch: MonkeyPatch) -> None:
    """An archive without exactly one .BIN member fails and keeps the live DB."""
    _mock_download(monkeypatch, make_zip({"README_LITE.TXT": b"readme"}))

    with pytest.raises(ValueError, match="exactly one .BIN member"):
        tasks.download_ip2location()

    assert db_path.read_bytes() == b"previous-live-database"
    assert not db_path.with_suffix(".zip.tmp").exists()
    assert not db_path.with_suffix(".bin.tmp").exists()


def test_download_rejects_non_zip_payload(db_path: Path, monkeypatch: MonkeyPatch) -> None:
    """A non-zip response body (e.g. an HTML error page) fails loudly."""
    _mock_download(monkeypatch, b"<html>token expired</html>")

    with pytest.raises(zipfile.BadZipFile):
        tasks.download_ip2location()

    assert db_path.read_bytes() == b"previous-live-database"
    assert not db_path.with_suffix(".zip.tmp").exists()


def test_download_rejects_database_failing_validation(db_path: Path, monkeypatch: MonkeyPatch) -> None:
    """A .BIN that fails the validation lookup must not replace the live DB."""
    payload = make_zip({"IP2LOCATION-LITE-DB5.IPV6.BIN": FAKE_BIN_CONTENT})
    _mock_download(monkeypatch, payload)
    monkeypatch.setattr(FakeIP2Location, "record", None)

    with pytest.raises(ValueError, match="validation lookup"):
        tasks.download_ip2location()

    assert db_path.read_bytes() == b"previous-live-database"
    assert not db_path.with_suffix(".bin.tmp").exists()

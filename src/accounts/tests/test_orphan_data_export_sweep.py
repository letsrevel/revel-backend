"""The sweep deletes export ZIPs that no export row names (#1476)."""

import os
import typing as t
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from accounts.models import RevelUser, UserDataExport
from accounts.service import gdpr
from accounts.tasks import sweep_orphan_data_exports
from accounts.tasks.gdpr import ORPHAN_DATA_EXPORT_MIN_AGE

pytestmark = pytest.mark.django_db

FOLDER = "protected/user_data_exports"
OLD = ORPHAN_DATA_EXPORT_MIN_AGE + timedelta(hours=1)
FRESH = ORPHAN_DATA_EXPORT_MIN_AGE - timedelta(hours=1)


@pytest.fixture(autouse=True)
def media(tmp_path: Path, settings: t.Any) -> Path:
    """An empty storage for each test."""
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


def _age(name: str, age: timedelta) -> None:
    moment = (timezone.now() - age).timestamp()
    os.utime(default_storage.path(name), (moment, moment))


def _file(name: str, age: timedelta) -> str:
    """Write a file of the given age into the storage; return its storage name."""
    saved = default_storage.save(name, ContentFile(b"zip"))
    _age(saved, age)
    return saved


def _generate(user: RevelUser) -> str:
    """Make a real export of ``user``; return the storage name of its ZIP."""
    name = gdpr.generate_user_data_export(user).file.name
    assert name
    return name


def _export_name() -> str:
    return f"{FOLDER}/revel_export_{uuid.uuid4()}.zip"


def test_the_upload_folder_of_the_export_field_is_the_folder_that_the_sweep_lists(user: RevelUser) -> None:
    name = _generate(user)

    assert name.startswith(f"{FOLDER}/revel_export_{user.pk}")
    assert default_storage.exists(name)


def test_an_old_file_without_a_row_is_deleted() -> None:
    orphan = _file(_export_name(), OLD)

    with patch("accounts.tasks.gdpr.logger") as log:
        assert sweep_orphan_data_exports() == {"files_deleted": 1, "files_failed": 0}

    assert not default_storage.exists(orphan)
    log.info.assert_called_once_with(
        "orphan_data_exports_swept", files_deleted=1, files_failed=0
    )  # counts only: no file name


@pytest.mark.parametrize("suffix", ["_AbC1234", "_" + "0f" * 8, "_" + "0f" * 8 + "_AbC1234"])
def test_every_form_of_the_export_file_name_is_an_export_file(suffix: str) -> None:
    """Older exports: ``revel_export_<id>.zip``, or with a storage suffix. Now: ``revel_export_<id>_<16 hex>.zip``."""
    orphan = _file(f"{FOLDER}/revel_export_{uuid.uuid4()}{suffix}.zip", OLD)

    assert sweep_orphan_data_exports() == {"files_deleted": 1, "files_failed": 0}

    assert not default_storage.exists(orphan)


def test_a_file_that_a_row_names_is_kept_also_when_it_is_old(user: RevelUser) -> None:
    name = _generate(user)
    _age(name, OLD)

    assert sweep_orphan_data_exports() == {"files_deleted": 0, "files_failed": 0}

    assert default_storage.exists(name)


def test_a_fresh_file_without_a_row_is_kept() -> None:
    """An export that is being made now has a file and no committed row yet."""
    fresh = _file(_export_name(), FRESH)

    assert sweep_orphan_data_exports() == {"files_deleted": 0, "files_failed": 0}

    assert default_storage.exists(fresh)


def test_files_outside_the_export_folder_and_other_files_in_it_are_not_touched() -> None:
    elsewhere = _file(f"protected/profile_pictures/revel_export_{uuid.uuid4()}.zip", OLD)
    parent = _file(f"protected/revel_export_{uuid.uuid4()}.zip", OLD)
    nested = _file(f"{FOLDER}/nested/revel_export_{uuid.uuid4()}.zip", OLD)
    other_name = _file(f"{FOLDER}/notes.txt", OLD)

    assert sweep_orphan_data_exports() == {"files_deleted": 0, "files_failed": 0}

    for name in (elsewhere, parent, nested, other_name):
        assert default_storage.exists(name)


def test_a_storage_without_the_export_folder_is_no_error() -> None:
    assert sweep_orphan_data_exports() == {"files_deleted": 0, "files_failed": 0}


def test_a_file_that_goes_away_during_the_sweep_is_skipped() -> None:
    orphan = _file(_export_name(), OLD)
    default_storage.delete(orphan)

    with patch.object(default_storage, "listdir", return_value=([], [orphan.rsplit("/", 1)[1]])):
        assert sweep_orphan_data_exports() == {"files_deleted": 0, "files_failed": 0}


def test_one_sweep_deletes_at_most_the_batch_size() -> None:
    orphans = [_file(_export_name(), OLD) for _ in range(3)]

    with patch("accounts.tasks.gdpr.ORPHAN_DATA_EXPORT_BATCH", 2):
        assert sweep_orphan_data_exports() == {"files_deleted": 2, "files_failed": 0}
    assert sum(default_storage.exists(name) for name in orphans) == 1

    assert sweep_orphan_data_exports() == {"files_deleted": 1, "files_failed": 0}


def test_an_export_whose_transaction_rolls_back_leaves_a_file_that_the_sweep_removes(user: RevelUser) -> None:
    """The worker dies after the ZIP is written and before the commit: the row is gone, the file is not."""
    with transaction.atomic():
        written = _generate(user)
        transaction.set_rollback(True)
    assert not UserDataExport.objects.filter(user=user).exists()
    assert default_storage.exists(written)

    assert sweep_orphan_data_exports() == {"files_deleted": 0, "files_failed": 0}  # still inside the lifetime of a link
    assert default_storage.exists(written)

    _age(written, OLD)
    assert sweep_orphan_data_exports() == {"files_deleted": 1, "files_failed": 0}
    assert not default_storage.exists(written)


def test_the_beat_row_runs_the_registered_task_once_a_day() -> None:
    row = PeriodicTask.objects.get(name="Sweep orphan data export files")

    assert row.task == sweep_orphan_data_exports.name == "accounts.sweep_orphan_data_exports"
    assert row.enabled
    assert (row.crontab.minute, row.crontab.hour, row.crontab.day_of_month) == ("40", "3", "*")


def test_a_file_that_cannot_be_deleted_does_not_stop_the_others() -> None:
    """One bad file must not block the sweep on every run; the failure is counted and logged without a name."""
    blocked, other = sorted(_file(_export_name(), OLD) for _ in range(2))
    real_delete = default_storage.delete

    def delete(name: str) -> None:
        if name == blocked:
            raise PermissionError(13, "Permission denied", default_storage.path(name))
        real_delete(name)

    with patch.object(default_storage, "delete", side_effect=delete), patch("accounts.tasks.gdpr.logger") as log:
        assert sweep_orphan_data_exports() == {"files_deleted": 1, "files_failed": 1}

    assert default_storage.exists(blocked)
    assert not default_storage.exists(other)
    log.error.assert_called_once_with("orphan_data_export_delete_failed", error_type="PermissionError")
    log.info.assert_called_once_with("orphan_data_exports_swept", files_deleted=1, files_failed=1)

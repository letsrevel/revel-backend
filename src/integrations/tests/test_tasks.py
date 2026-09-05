"""Celery task wrappers: names pinned, retry class wired, missing link tolerated."""

import uuid

from integrations import tasks
from integrations.exceptions import RetryableProviderError


def test_task_names_are_pinned() -> None:
    assert tasks.push_event_link.name == "integrations.push_event_link"
    assert tasks.import_remote_event.name == "integrations.import_remote_event"


def test_push_task_retries_only_retryable() -> None:
    assert tasks.push_event_link.autoretry_for == (RetryableProviderError,)  # type: ignore[attr-defined]


def test_push_task_ignores_missing_link(db: None) -> None:
    tasks.push_event_link(str(uuid.uuid4()))  # no raise: link deleted between dispatch and run

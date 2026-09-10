"""Tests for the Telegram Celery tasks.

The most important case here is the *deny* path of the callback allowlist:
``_execute_callback`` receives a module path and a function name from the Celery
message and resolves them with ``importlib.import_module`` + ``getattr``, so the
allowlist is the only thing standing between a forged/poisoned task payload and
arbitrary code execution.

Note: ``src/conftest.py`` autouse-monkeypatches ``telegram.tasks.send_message_task``.
The module-level imports below bind the *real* objects, so tests must always use
those names rather than ``telegram.tasks.<name>`` attribute access.
"""

import typing as t
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.methods import SendMessage
from pytest import MonkeyPatch

from accounts.models import RevelUser
from notifications.enums import DeliveryStatus
from telegram.bot import get_bot, get_dispatcher, get_storage
from telegram.models import TelegramUser
from telegram.tasks import (
    _ALLOWED_TELEGRAM_CALLBACKS,
    _execute_callback,
    send_broadcast_message_task,
    send_message_task,
)

ALLOWED_MODULE = "notifications.service.channels.telegram"
ALLOWED_FUNCTION = "update_delivery_status"


@pytest.fixture
def telegram_user(django_user_model: type[RevelUser]) -> TelegramUser:
    """A linked Telegram user."""
    user = django_user_model.objects.create_user(username="tg@example.com", email="tg@example.com", password="pw")
    return TelegramUser.objects.create(user=user, telegram_id=987654)


# --- Callback allowlist (security control) ---


def test_allowlist_contains_only_the_delivery_status_callback() -> None:
    """The allowlist is the security boundary — pin its exact contents."""
    assert _ALLOWED_TELEGRAM_CALLBACKS == frozenset({(ALLOWED_MODULE, ALLOWED_FUNCTION)})


@pytest.mark.parametrize(
    "callback_data",
    [
        pytest.param({"module": "os", "function": "system", "kwargs": {}}, id="arbitrary-module"),
        pytest.param({"module": "builtins", "function": "eval", "kwargs": {}}, id="builtins-eval"),
        pytest.param(
            {"module": ALLOWED_MODULE, "function": "NotificationDelivery", "kwargs": {}},
            id="allowed-module-other-function",
        ),
        pytest.param(
            {"module": "notifications.service.channels", "function": ALLOWED_FUNCTION, "kwargs": {}},
            id="other-module-allowed-function",
        ),
    ],
)
def test_execute_callback_blocks_callbacks_outside_the_allowlist(
    monkeypatch: MonkeyPatch, callback_data: dict[str, t.Any]
) -> None:
    """A non-allowlisted (module, function) pair never reaches import_module/getattr."""
    import_module = MagicMock()
    monkeypatch.setattr("telegram.tasks.importlib.import_module", import_module)

    _execute_callback(callback_data, False, None)

    import_module.assert_not_called()


@pytest.mark.parametrize(
    "callback_data",
    [
        pytest.param({}, id="empty"),
        pytest.param({"function": ALLOWED_FUNCTION}, id="missing-module"),
        pytest.param({"module": ALLOWED_MODULE}, id="missing-function"),
        pytest.param({"module": "", "function": ALLOWED_FUNCTION}, id="blank-module"),
    ],
)
def test_execute_callback_rejects_incomplete_callback_data(
    monkeypatch: MonkeyPatch, callback_data: dict[str, t.Any]
) -> None:
    """Callback data missing module or function short-circuits before resolution."""
    import_module = MagicMock()
    monkeypatch.setattr("telegram.tasks.importlib.import_module", import_module)

    _execute_callback(callback_data, False, None)

    import_module.assert_not_called()


def test_execute_callback_runs_allowlisted_callback_on_success(monkeypatch: MonkeyPatch) -> None:
    """The one allowlisted callback runs and is told the message was SENT."""
    callback = MagicMock()
    monkeypatch.setattr(f"{ALLOWED_MODULE}.{ALLOWED_FUNCTION}", callback)

    _execute_callback(
        {"module": ALLOWED_MODULE, "function": ALLOWED_FUNCTION, "kwargs": {"delivery_id": "abc"}},
        False,
        None,
    )

    callback.assert_called_once_with(delivery_id="abc", status=DeliveryStatus.SENT)


def test_execute_callback_reports_failure_with_error_message(monkeypatch: MonkeyPatch) -> None:
    """On the error branch the callback receives FAILED plus the error message."""
    callback = MagicMock()
    monkeypatch.setattr(f"{ALLOWED_MODULE}.{ALLOWED_FUNCTION}", callback)

    _execute_callback(
        {"module": ALLOWED_MODULE, "function": ALLOWED_FUNCTION, "kwargs": {"delivery_id": "abc"}},
        True,
        "User blocked the bot",
    )

    callback.assert_called_once_with(
        delivery_id="abc",
        status=DeliveryStatus.FAILED,
        error_message="User blocked the bot",
    )


def test_execute_callback_swallows_callback_errors(monkeypatch: MonkeyPatch) -> None:
    """A failing callback must not take the sending task down with it."""
    callback = MagicMock(side_effect=RuntimeError("db gone"))
    monkeypatch.setattr(f"{ALLOWED_MODULE}.{ALLOWED_FUNCTION}", callback)

    _execute_callback({"module": ALLOWED_MODULE, "function": ALLOWED_FUNCTION, "kwargs": {}}, False, None)

    callback.assert_called_once()


# --- send_message_task ---


def test_send_message_task_sends_and_runs_callback(monkeypatch: MonkeyPatch) -> None:
    """The happy path forwards the message and reports SENT through the callback."""
    send = AsyncMock()
    monkeypatch.setattr("telegram.utils.send_telegram_message", send)
    callback = MagicMock()
    monkeypatch.setattr(f"{ALLOWED_MODULE}.{ALLOWED_FUNCTION}", callback)

    send_message_task(
        123,
        message="hello",
        callback_data={"module": ALLOWED_MODULE, "function": ALLOWED_FUNCTION, "kwargs": {"delivery_id": "d1"}},
    )

    send.assert_awaited_once()
    await_args = send.await_args
    assert await_args is not None
    assert await_args.args[0] == 123
    assert await_args.kwargs["message"] == "hello"
    assert await_args.kwargs["photo"] is None
    callback.assert_called_once_with(delivery_id="d1", status=DeliveryStatus.SENT)


def test_send_message_task_attaches_qr_photo_and_keyboard(monkeypatch: MonkeyPatch) -> None:
    """``qr_data`` becomes a photo and ``reply_markup`` is deserialized to a markup."""
    send = AsyncMock()
    monkeypatch.setattr("telegram.utils.send_telegram_message", send)
    monkeypatch.setattr("telegram.utils.generate_qr_code", MagicMock(return_value=b"png-bytes"))

    send_message_task(
        123,
        message="ticket",
        reply_markup={"inline_keyboard": [[{"text": "Open", "url": "https://example.com"}]]},
        qr_data="TICKET-1",
    )

    await_args = send.await_args
    assert await_args is not None
    assert await_args.kwargs["photo"] == b"png-bytes"
    keyboard = await_args.kwargs["reply_markup"]
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].text == "Open"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("telegram_message", "flag", "expected_error"),
    [
        ("Forbidden: bot was blocked by the user", "blocked_by_user", "User blocked the bot"),
        ("Forbidden: user is deactivated", "user_is_deactivated", "User is deactivated"),
    ],
)
def test_send_message_task_marks_unreachable_users(
    monkeypatch: MonkeyPatch,
    telegram_user: TelegramUser,
    telegram_message: str,
    flag: str,
    expected_error: str,
) -> None:
    """Blocked/deactivated users are flagged and reported as FAILED, not raised."""
    monkeypatch.setattr(
        "telegram.utils.send_telegram_message",
        AsyncMock(
            side_effect=TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message=telegram_message)
        ),
    )
    callback = MagicMock()
    monkeypatch.setattr(f"{ALLOWED_MODULE}.{ALLOWED_FUNCTION}", callback)

    send_message_task(
        telegram_user.telegram_id,
        message="hello",
        callback_data={"module": ALLOWED_MODULE, "function": ALLOWED_FUNCTION, "kwargs": {}},
    )

    telegram_user.refresh_from_db()
    assert getattr(telegram_user, flag) is True
    callback.assert_called_once_with(status=DeliveryStatus.FAILED, error_message=expected_error)


@pytest.mark.django_db
def test_send_message_task_reports_other_forbidden_errors_verbatim(
    monkeypatch: MonkeyPatch, telegram_user: TelegramUser
) -> None:
    """An unrecognised Forbidden error leaves the user untouched but still reports FAILED."""
    monkeypatch.setattr(
        "telegram.utils.send_telegram_message",
        AsyncMock(
            side_effect=TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="Forbidden: nope")
        ),
    )
    callback = MagicMock()
    monkeypatch.setattr(f"{ALLOWED_MODULE}.{ALLOWED_FUNCTION}", callback)

    send_message_task(
        telegram_user.telegram_id,
        message="hello",
        callback_data={"module": ALLOWED_MODULE, "function": ALLOWED_FUNCTION, "kwargs": {}},
    )

    telegram_user.refresh_from_db()
    assert telegram_user.blocked_by_user is False
    assert telegram_user.user_is_deactivated is False
    assert callback.call_args.kwargs["status"] == DeliveryStatus.FAILED


def test_send_message_task_retries_on_rate_limit(monkeypatch: MonkeyPatch) -> None:
    """A Telegram rate limit routes into ``self.retry`` instead of the generic handler.

    Called directly (no worker request), Celery's ``Task.retry`` re-raises the original
    exception rather than ``Retry`` — so the assertion here is that the flood error
    propagates *without* the generic-exception branch running its callback.
    """
    monkeypatch.setattr(
        "telegram.utils.send_telegram_message",
        AsyncMock(
            side_effect=TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="flood", retry_after=7)
        ),
    )
    callback = MagicMock()
    monkeypatch.setattr(f"{ALLOWED_MODULE}.{ALLOWED_FUNCTION}", callback)

    with pytest.raises(TelegramRetryAfter):
        send_message_task(
            123,
            message="hello",
            callback_data={"module": ALLOWED_MODULE, "function": ALLOWED_FUNCTION, "kwargs": {}},
        )

    callback.assert_not_called()


def test_send_message_task_reraises_unexpected_errors_after_callback(monkeypatch: MonkeyPatch) -> None:
    """Unexpected failures still run the callback, then propagate so Celery marks a failure."""
    monkeypatch.setattr("telegram.utils.send_telegram_message", AsyncMock(side_effect=ValueError("boom")))
    callback = MagicMock()
    monkeypatch.setattr(f"{ALLOWED_MODULE}.{ALLOWED_FUNCTION}", callback)

    with pytest.raises(ValueError, match="boom"):
        send_message_task(
            123,
            message="hello",
            callback_data={"module": ALLOWED_MODULE, "function": ALLOWED_FUNCTION, "kwargs": {}},
        )

    callback.assert_called_once_with(status=DeliveryStatus.FAILED, error_message="boom")


# --- send_broadcast_message_task ---


@pytest.mark.django_db
def test_send_broadcast_message_task_queues_every_active_user(telegram_user: TelegramUser) -> None:
    """The broadcast fans out to active users and returns how many were queued.

    ``send_message_task`` is the suite-wide autouse mock here, so this asserts the
    fan-out, not the delivery.
    """
    assert send_broadcast_message_task("hello everyone") == 1


# --- bot construction smoke test ---


def test_bot_and_dispatcher_construct() -> None:
    """``get_bot``/``get_storage``/``get_dispatcher`` build a wired-up dispatcher."""
    bot = get_bot(token="123456:AAHtest-token-value")
    dispatcher = get_dispatcher(get_storage("memory"))

    assert bot.token == "123456:AAHtest-token-value"
    assert dispatcher.sub_routers


@pytest.mark.parametrize(
    ("storage", "debug", "expected"),
    [
        ("memory", False, MemoryStorage),
        ("redis", True, RedisStorage),
        (None, True, MemoryStorage),
        (None, False, RedisStorage),
    ],
)
def test_get_storage_picks_backend_by_argument_then_debug(
    settings: t.Any,
    storage: t.Literal["memory", "redis"] | None,
    debug: bool,
    expected: type,
) -> None:
    """An explicit argument wins; otherwise DEBUG decides memory vs Redis."""
    settings.DEBUG = debug

    assert isinstance(get_storage(storage), expected)

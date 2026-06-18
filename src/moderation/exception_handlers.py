"""Moderation exception handlers, registered from ModerationConfig.ready."""

from common.exception_handlers import ExceptionHandler, make_simple_handler, register_handlers
from moderation.exceptions import FoodItemBlockedError

HANDLERS: dict[type[Exception], ExceptionHandler] = {
    FoodItemBlockedError: make_simple_handler(422),
}


def register() -> None:
    """Install moderation exception handlers on the global Ninja API."""
    from api.api import api

    register_handlers(api, HANDLERS)

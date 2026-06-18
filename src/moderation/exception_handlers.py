"""Moderation exception handlers, registered from ModerationConfig.ready."""

from django.utils.translation import gettext_lazy as _

from common.exception_handlers import ExceptionHandler, make_static_handler, register_handlers
from moderation.exceptions import FoodItemBlockedError

HANDLERS: dict[type[Exception], ExceptionHandler] = {
    FoodItemBlockedError: make_static_handler(422, _("This name is not allowed.")),
}


def register() -> None:
    """Install moderation exception handlers on the global Ninja API."""
    from api.api import api

    register_handlers(api, HANDLERS)

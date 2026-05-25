"""Polls controllers package.

Splits poll endpoints into logical groupings while preserving the
original OpenAPI ordering of the previous single-module controller.
"""

from .poll_controller import PollController
from .question_controller import PollQuestionController

# Controllers in order to preserve OpenAPI grouping.
POLL_CONTROLLERS: list[type] = [
    PollController,
    PollQuestionController,
]

__all__ = ["POLL_CONTROLLERS", "PollController", "PollQuestionController"]

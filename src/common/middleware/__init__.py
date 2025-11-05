"""Common middleware for Revel."""

from .language import UserLanguageMiddleware
from .observability import StructlogContextMiddleware

__all__ = ["StructlogContextMiddleware", "UserLanguageMiddleware"]

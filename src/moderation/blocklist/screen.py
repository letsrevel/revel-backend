from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from moderation.blocklist.loader import load_blocklist
from moderation.blocklist.normalize import tokens


class NameNotAllowed(HttpError):
    """A user-supplied name matched the blocklist. Subclasses HttpError so it renders as 422."""

    def __init__(self) -> None:
        """Render as HTTP 422 with the translated message."""
        super().__init__(422, str(_("This name is not allowed.")))


def is_blocked(text: str, *, wordlist: frozenset[str] | None = None) -> bool:
    """Return True if the text exactly matches a blocklist term after normalization.

    Matching is per normalized token plus adjacent bigram (to catch a term split across
    two tokens once separators are stripped). Exact match only — no fuzzy matching.
    """
    words = wordlist if wordlist is not None else load_blocklist()
    if not words:
        return False
    toks = tokens(text)
    candidates = toks + ["".join(pair) for pair in zip(toks, toks[1:])]
    return any(c in words for c in candidates)


def assert_name_allowed(name: str) -> None:
    """Raise NameNotAllowed (422) if the name is on the blocklist."""
    if is_blocked(name):
        raise NameNotAllowed

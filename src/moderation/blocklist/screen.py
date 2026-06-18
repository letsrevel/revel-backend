from moderation.blocklist.loader import load_blocklist
from moderation.blocklist.normalize import tokens


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

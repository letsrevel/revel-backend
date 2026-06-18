import enum

from rapidfuzz import fuzz

from moderation.blocklist.loader import load_blocklist
from moderation.blocklist.normalize import tokens


class Verdict(enum.Enum):
    BLOCK = "block"
    ESCALATE = "escalate"
    ALLOW = "allow"


def screen(
    text: str,
    *,
    wordlist: frozenset[str] | None = None,
    escalate_floor: int = 80,
) -> tuple[Verdict, float]:
    """Tier a candidate string.

    Exact normalized-token match → BLOCK; fuzzy in band → ESCALATE.
    """
    words = wordlist if wordlist is not None else load_blocklist()
    if not words:
        return Verdict.ALLOW, 0.0

    toks = tokens(text)
    # bigrams catch slurs split across two tokens after separator-stripping
    candidates = toks + ["".join(pair) for pair in zip(toks, toks[1:])]
    if not candidates:
        return Verdict.ALLOW, 0.0

    # BLOCK: exact normalized match
    if any(c in words for c in candidates):
        return Verdict.BLOCK, 100.0

    # ESCALATE: best fuzzy ratio across candidate × wordlist
    best = 0.0
    for c in candidates:
        for term in words:
            ratio = fuzz.ratio(c, term)
            if ratio > best:
                best = ratio
    if best >= escalate_floor:
        return Verdict.ESCALATE, best
    return Verdict.ALLOW, best

import re
import unicodedata

_LEET = str.maketrans({"3": "e", "0": "o", "1": "i", "4": "a", "5": "s", "7": "t", "$": "s", "@": "a"})
_REPEAT = re.compile(r"(.)\1{2,}")  # 3+ identical chars
_NON_ALNUM = re.compile(r"[^a-z0-9\s]")  # keep spaces for tokenization


def normalize_text(text: str) -> str:
    """Canonicalize text to defeat common evasion before matching."""
    text = text.lower()
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = text.translate(_LEET)
    text = _REPEAT.sub(r"\1", text)
    text = _NON_ALNUM.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    """Normalized tokens (whitespace-split)."""
    normalized = normalize_text(text)
    return normalized.split() if normalized else []

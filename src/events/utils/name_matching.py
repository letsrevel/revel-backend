"""Name normalization for matching register entries to accounts (#1319)."""

import unicodedata


def normalize_name_for_matching(name: str) -> str:
    """Lowercase, accent-free, whitespace-collapsed form of ``name``: equal results mean "same name"."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())

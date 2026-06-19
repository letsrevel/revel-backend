import functools
import pathlib

from moderation.blocklist.normalize import normalize_text

_DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "blocklist"
_LANGS = ("en", "de", "it", "fr")


@functools.lru_cache(maxsize=1)
def load_blocklist() -> frozenset[str]:
    """Load and normalize all language wordlists into one frozenset (memoized)."""
    terms: set[str] = set()
    for lang in _LANGS:
        path = _DATA_DIR / f"{lang}.txt"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            normalized = normalize_text(line)
            if normalized:
                terms.add(normalized)
    return frozenset(terms)

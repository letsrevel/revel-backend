import pytest

from moderation.blocklist.normalize import normalize_text, tokens


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Peanút", "peanut"),
        ("p3an0t", "peanot"),
        ("peeeanut", "peanut"),
        ("p.e.a.n.u.t", "peanut"),
        ("  Chicken   Soup ", "chicken soup"),
        # every leet substitution: 3→e 0→o 1→i 4→a 5→s 7→t $→s @→a
        ("3014577", "eoiastt"),
        ("$@", "sa"),
        # combined evasion: diacritics + leet + repeats + separators together
        ("Ç-h33ck@@@n", "cheeckan"),
    ],
)
def test_normalize_text(raw: str, expected: str) -> None:
    assert normalize_text(raw) == expected


def test_tokens_splits_words() -> None:
    assert tokens("Chicken  Soup") == ["chicken", "soup"]


@pytest.mark.parametrize("raw", ["", "   ", "...", "\t\n"])
def test_tokens_empty_for_blank_input(raw: str) -> None:
    assert tokens(raw) == []

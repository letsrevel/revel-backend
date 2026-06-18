import pytest

from moderation.blocklist.screen import is_blocked

WORDS = frozenset({"badword", "slurword"})


@pytest.mark.parametrize(
    "text,blocked",
    [
        ("badword", True),  # exact
        ("chicken badword soup", True),  # embedded token
        ("b4dw0rd", True),  # leet → normalizes to badword
        ("badwrd", False),  # near miss — fuzzy matching is gone, so NOT blocked
        ("chicken soup", False),  # benign
    ],
)
def test_is_blocked(text: str, blocked: bool) -> None:
    assert is_blocked(text, wordlist=WORDS) is blocked


def test_empty_wordlist_allows() -> None:
    assert is_blocked("badword", wordlist=frozenset()) is False


def test_scunthorpe_guard() -> None:
    # token-level exact match means a benign word containing a blocked substring is not blocked
    assert is_blocked("scunthorpe", wordlist=frozenset({"cunt"})) is False

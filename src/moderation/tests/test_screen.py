import pytest

from moderation.blocklist.screen import Verdict, screen

WORDS = frozenset({"badword", "slurword"})


@pytest.mark.parametrize(
    "text,expected",
    [
        ("badword", Verdict.BLOCK),  # exact
        ("chicken badword soup", Verdict.BLOCK),  # embedded token
        ("b4dw0rd", Verdict.BLOCK),  # leet → normalizes to badword
        ("badwrd", Verdict.ESCALATE),  # near miss (fuzzy ~92.3, in [80, 100))
        ("chicken soup", Verdict.ALLOW),  # benign (max ratio ~33.3, below floor)
    ],
)
def test_screen_tiers(text: str, expected: Verdict) -> None:
    verdict, _ratio = screen(text, wordlist=WORDS)
    assert verdict == expected


def test_empty_wordlist_allows() -> None:
    verdict, _ = screen("badword", wordlist=frozenset())
    assert verdict == Verdict.ALLOW


def test_scunthorpe_guard() -> None:
    # a legit word that merely contains/neighbours a blocked token must not BLOCK
    verdict, _ = screen("scunthorpe", wordlist=frozenset({"cunt"}))
    assert verdict != Verdict.BLOCK

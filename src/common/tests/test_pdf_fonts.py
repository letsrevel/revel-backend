from pathlib import Path

FONTS = Path(__file__).resolve().parents[2] / "fonts"


def test_nata_sans_bundled() -> None:
    assert (FONTS / "NataSans-Light.ttf").exists()
    assert (FONTS / "NataSans-SemiBold.ttf").exists()

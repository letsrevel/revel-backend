from pathlib import Path

from PIL import Image

LOGO = Path(__file__).resolve().parents[4] / "revel-frontend" / "static" / "revel-email-logo.png"


def test_email_logo_exists_and_is_white_on_transparent() -> None:
    assert LOGO.exists(), "white email logo not generated"
    img = Image.open(LOGO).convert("RGBA")
    assert img.width <= 960 and img.height <= 960, "logo too large for email"
    # sample opaque pixels; their RGB must be white
    opaque = [px[:3] for px in list(img.getdata()) if px[3] > 200]
    assert opaque, "logo has no opaque pixels"
    assert all(r > 240 and g > 240 and b > 240 for r, g, b in opaque[:5000])

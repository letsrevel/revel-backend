"""Account emails link to the frontend's password-reset form at ``/password-reset``."""

import pytest
from django.template.loader import render_to_string


@pytest.mark.parametrize("base", ["totp_locked", "email_change_notice"])
@pytest.mark.parametrize("ext", ["html", "txt"])
def test_reset_link_points_at_the_frontend_route(base: str, ext: str) -> None:
    rendered = render_to_string(
        f"accounts/emails/{base}_body.{ext}",
        {"frontend_base_url": "https://letsrevel.io", "user": None, "new_email": "new@example.com"},
    )
    assert "https://letsrevel.io/password-reset" in rendered
    assert "/account/password-reset" not in rendered

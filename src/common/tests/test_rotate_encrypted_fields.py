"""``rotate_encrypted_fields``: re-encrypt stored ciphertext under the primary key after a rotation."""

import typing as t
from io import StringIO

import pyotp
import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import F, TextField
from django.db.models.functions import Cast
from encrypted_fields.fields import EncryptedFieldMixin

from accounts.models import RevelUser
from events.models import Organization
from integrations.models import PlatformConnection

pytestmark = pytest.mark.django_db

SECRET_A, SECRET_B = "secret-A", "secret-B"
SALT_1, SALT_2 = "salt-one", "salt-two"


def _encrypted_fields() -> list[EncryptedFieldMixin]:
    return [
        f
        for m in apps.get_models()
        for f in m._meta.concrete_fields
        if isinstance(f, EncryptedFieldMixin) and f.model is m
    ]


def use_keys(settings: t.Any, *, secret: str, salts: list[str], secret_fallbacks: list[str] | None = None) -> None:
    """Switch key settings and drop the keys cached on the shared field instances."""
    settings.SECRET_KEY = secret
    settings.SECRET_KEY_FALLBACKS = secret_fallbacks or []
    settings.SALT_KEY = salts
    clear_field_key_caches()


def clear_field_key_caches() -> None:
    """``keys``/``f`` are cached_property on the shared field instance; drop them."""
    for field in _encrypted_fields():
        field.__dict__.pop("keys", None)
        field.__dict__.pop("f", None)


def _raw(model: t.Any, name: str, pk: t.Any) -> str:
    return str(model.objects.annotate(r=Cast(F(name), TextField())).values_list("r", flat=True).get(pk=pk))


def _run(*args: str) -> str:
    out = StringIO()
    call_command("rotate_encrypted_fields", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def rows(settings: t.Any, revel_user_factory: t.Any) -> t.Iterator[tuple[RevelUser, PlatformConnection]]:
    use_keys(settings, secret=SECRET_A, salts=[SALT_1])
    user = revel_user_factory()
    org = Organization.objects.create(name="o", slug="o", owner=user)
    conn = PlatformConnection.objects.create(organization=org, provider="x", access_token="tok-1", refresh_token=None)
    yield user, conn
    clear_field_key_caches()  # recomputed from the restored settings on next use


def test_discovers_all_encrypted_fields() -> None:
    labels = {f"{f.model._meta.label}.{f.name}" for f in _encrypted_fields()}
    assert {"accounts.RevelUser.totp_secret", "integrations.PlatformConnection.access_token"} <= labels


def test_secret_rotation_end_to_end(settings: t.Any, rows: tuple[RevelUser, PlatformConnection]) -> None:
    user, conn = rows
    secret = RevelUser.objects.get(pk=user.pk).totp_secret
    before = _raw(RevelUser, "totp_secret", user.pk)

    use_keys(settings, secret=SECRET_B, salts=[SALT_1], secret_fallbacks=[SECRET_A])
    assert RevelUser.objects.get(pk=user.pk).totp_secret == secret  # fallback decrypts

    out = _run("--dry-run")
    assert "accounts.RevelUser.totp_secret" in out
    assert secret not in out
    assert _raw(RevelUser, "totp_secret", user.pk) == before  # dry-run writes nothing
    with pytest.raises(CommandError):
        _run("--verify")  # still on the old key

    _run()
    after = _raw(RevelUser, "totp_secret", user.pk)
    assert after != before
    _run("--verify")  # primary key only

    again = _run()
    assert _raw(RevelUser, "totp_secret", user.pk) == after  # idempotent: already current rows untouched
    assert "rotated=0" in again

    use_keys(settings, secret=SECRET_B, salts=[SALT_1])  # fallbacks dropped
    fresh = RevelUser.objects.get(pk=user.pk)
    assert fresh.totp_secret == secret
    assert pyotp.TOTP(fresh.totp_secret).verify(pyotp.TOTP(secret).now())
    assert PlatformConnection.objects.get(pk=conn.pk).access_token == "tok-1"
    assert PlatformConnection.objects.get(pk=conn.pk).refresh_token is None


def test_salt_rotation(settings: t.Any, rows: tuple[RevelUser, PlatformConnection]) -> None:
    user, _conn = rows
    secret = RevelUser.objects.get(pk=user.pk).totp_secret
    before = _raw(RevelUser, "totp_secret", user.pk)

    use_keys(settings, secret=SECRET_A, salts=[SALT_2, SALT_1])
    assert RevelUser.objects.get(pk=user.pk).totp_secret == secret
    _run()
    assert _raw(RevelUser, "totp_secret", user.pk) != before
    _run("--verify")

    use_keys(settings, secret=SECRET_A, salts=[SALT_2])
    assert RevelUser.objects.get(pk=user.pk).totp_secret == secret


def test_single_key_has_nothing_to_rotate(rows: tuple[RevelUser, PlatformConnection]) -> None:
    assert "nothing to rotate" in _run()

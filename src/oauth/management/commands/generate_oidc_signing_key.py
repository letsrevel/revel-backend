"""Generate an RSA-2048 private key PEM for the OIDC provider."""

import typing as t
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.management.base import BaseCommand, CommandParser


class Command(BaseCommand):
    help = "Write an RSA-2048 private key PEM to --out (default certs/oidc.pem)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the command's arguments."""
        parser.add_argument("--out", default="certs/oidc.pem")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Write a fresh RSA-2048 key, refusing to clobber an existing file."""
        out = Path(options["out"])
        if out.exists():
            self.stderr.write(f"{out} already exists; refusing to overwrite.")
            return
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )
        out.chmod(0o600)
        self.stdout.write(f"Wrote {out} (mode 0600).")
        self.stdout.write(
            "If it is mounted into a container that runs as another uid (the Docker image uses 997), "
            "make it readable there — e.g. `chmod 644` — or the provider stays disabled and "
            "`manage.py check` reports oauth.E002."
        )

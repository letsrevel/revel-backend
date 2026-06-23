"""``python manage.py bootstrap_admin``.

Interactive first-run bootstrap for a self-hosted instance. Prompts for an admin
email, password and organization name (proposing a slug), then creates:

- the initial **superuser**, with ``username`` pinned to the email so it matches
  every account created through the public API (which uses the email as the
  username); and
- the **first organization** owned by that user.

Finally it prints the admin-panel and frontend URLs, both derived from settings
(``BASE_URL`` / ``ADMIN_URL`` / ``FRONTEND_BASE_URL``) rather than hardcoded, so
the links match whatever domains the instance was configured with.
"""

import getpass
import typing as t

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.core.validators import validate_email
from django.db import transaction
from django.utils.text import slugify
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Organization
from events.service.organization_service import create_organization


class Command(BaseCommand):
    """Interactively create the initial superuser and first organization."""

    help = "Interactively create the initial superuser and first organization for a self-hosted instance."

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Prompt for the admin/org details, create them, and print the URLs."""
        self.stdout.write(self.style.MIGRATE_HEADING("Revel first-run setup"))

        email = self._prompt_email()
        password = self._prompt_password(email)
        org_name = self._prompt_org_name()
        slug = self._prompt_slug(org_name)

        try:
            with transaction.atomic():
                user = RevelUser.objects.create_superuser(username=email, email=email, password=password)
                user.email_verified = True  # auto-verifies the org contact email; suppresses the verification mail
                user.save(update_fields=["email_verified"])
                org = create_organization(owner=user, name=org_name, contact_email=email, slug=slug)
        except HttpError as exc:  # create_organization signals validation failures as HttpError
            self.stderr.write(self.style.ERROR(f"Could not create the organization: {exc}"))
            return

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Created superuser '{email}' and organization '{org.name}'."))
        if settings.ADMIN_URL:
            admin_url = f"{settings.BASE_URL.rstrip('/')}/{settings.ADMIN_URL.lstrip('/')}"
            self.stdout.write(f"  Admin panel: {admin_url}")
        self.stdout.write(f"  Frontend:    {settings.FRONTEND_BASE_URL.rstrip('/')}")

    # -- prompts ---------------------------------------------------------------

    def _prompt_email(self) -> str:
        while True:
            email = input("Admin email: ").strip()
            try:
                validate_email(email)
            except ValidationError:
                self.stderr.write("  Not a valid email address.")
                continue
            if RevelUser.objects.filter(username__iexact=email).exists():
                self.stderr.write("  A user with that email already exists.")
                continue
            return email

    def _prompt_password(self, email: str) -> str:
        while True:
            password = getpass.getpass("Password: ")
            if password != getpass.getpass("Password (again): "):
                self.stderr.write("  Passwords do not match.")
                continue
            try:
                validate_password(password, user=RevelUser(username=email, email=email))
            except ValidationError as exc:
                for message in exc.messages:
                    self.stderr.write(f"  {message}")
                continue
            return password

    def _prompt_org_name(self) -> str:
        while True:
            name = input("Organization name: ").strip()
            if name:
                return name
            self.stderr.write("  Organization name cannot be empty.")

    def _prompt_slug(self, org_name: str) -> str:
        suggested = slugify(org_name)
        while True:
            slug = slugify(input(f"Organization slug [{suggested}]: ").strip() or suggested)
            if not slug:
                self.stderr.write("  Slug cannot be empty.")
                continue
            if Organization.objects.filter(slug=slug).exists():
                self.stderr.write(f"  Slug '{slug}' is already taken.")
                continue
            return slug

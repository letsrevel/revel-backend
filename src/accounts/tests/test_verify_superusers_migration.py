"""Migration 0038 marks existing superusers as verified, and nobody else."""

import importlib

import pytest
from django.apps import apps as django_apps

from accounts.models import RevelUser

pytestmark = pytest.mark.django_db

verify_superusers = importlib.import_module("accounts.migrations.0038_verify_existing_superusers").verify_superusers


def test_only_superusers_are_marked_verified() -> None:
    admin = RevelUser.objects.create_superuser(username="root@example.com", email="root@example.com", password="x")
    staff = RevelUser.objects.create_user(username="staff@example.com", email="staff@example.com", password="x")
    member = RevelUser.objects.create_user(username="m@example.com", email="m@example.com", password="x")
    guest = RevelUser.objects.create_user(username="g@example.com", email="g@example.com", password="x")
    RevelUser.objects.filter(pk=guest.pk).update(is_superuser=True, guest=True, email_verified=False)
    RevelUser.objects.filter(pk=admin.pk).update(email_verified=False)
    RevelUser.objects.filter(pk=staff.pk).update(is_staff=True, email_verified=False)
    RevelUser.objects.filter(pk=member.pk).update(email_verified=False)

    verify_superusers(django_apps, None)

    assert RevelUser.objects.get(pk=admin.pk).email_verified is True
    assert RevelUser.objects.get(pk=staff.pk).email_verified is False
    assert RevelUser.objects.get(pk=member.pk).email_verified is False
    assert RevelUser.objects.get(pk=guest.pk).email_verified is False


def test_new_superusers_count_as_verified() -> None:
    admin = RevelUser.objects.create_superuser(username="new-root@example.com", email="new-root@example.com")
    assert admin.email_verified is True

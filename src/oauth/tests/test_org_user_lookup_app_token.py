"""The org-admin user lookup (an account-existence oracle) is never reachable through OAuth app tokens."""

import pytest
from django.test.client import Client
from django.urls import reverse

from conftest import RevelUserFactory
from events.models import Organization
from oauth.models import OAuthApplication
from oauth.scopes import SCOPES
from oauth.tests.test_auth_class import make_access_token

pytestmark = pytest.mark.django_db


def test_app_token_cannot_reach_user_lookup(
    organization: Organization, oauth_app: OAuthApplication, revel_user_factory: RevelUserFactory
) -> None:
    target = revel_user_factory(email="target@example.com", email_verified=True)
    url = reverse("api:lookup_organization_user", kwargs={"slug": organization.slug})

    for scopes in ("org:read org:members", " ".join(sorted(SCOPES))):
        client = Client(HTTP_AUTHORIZATION=f"Bearer {make_access_token(organization.owner, oauth_app, scopes)}")
        response = client.get(url, {"email": "target@example.com"})
        assert response.status_code == 401, response.status_code
        assert str(target.pk) not in response.content.decode()

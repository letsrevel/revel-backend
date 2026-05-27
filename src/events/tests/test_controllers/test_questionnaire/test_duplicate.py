"""Integration tests for POST /questionnaires/{id}/duplicate endpoint."""

import orjson
import pytest
from django.test import Client
from django.urls import reverse

from events.models import Organization, OrganizationQuestionnaire, OrganizationStaff, PermissionMap, PermissionsSchema
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_org_questionnaire(organization: Organization, name: str = "Template") -> OrganizationQuestionnaire:
    """Create a published OrganizationQuestionnaire owned by *organization*.

    Args:
        organization: The owning organization.
        name: The questionnaire name.

    Returns:
        A new OrganizationQuestionnaire instance.
    """
    q = Questionnaire.objects.create(name=name, status=Questionnaire.QuestionnaireStatus.PUBLISHED)
    return OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q)


def _duplicate_url(oq: OrganizationQuestionnaire) -> str:
    """Return the duplicate endpoint URL for the given OrganizationQuestionnaire.

    Args:
        oq: The OrganizationQuestionnaire to build the URL for.

    Returns:
        The URL string.
    """
    return reverse("api:duplicate_org_questionnaire", kwargs={"org_questionnaire_id": oq.pk})


# ---------------------------------------------------------------------------
# Success cases
# ---------------------------------------------------------------------------


def test_duplicate_questionnaire_by_owner(
    organization: Organization,
    organization_owner_client: Client,
) -> None:
    """Organization owner can duplicate a questionnaire."""
    oq = _make_org_questionnaire(organization, name="My Q")
    payload = {"name": "My Q Copy"}

    response = organization_owner_client.post(
        _duplicate_url(oq), data=orjson.dumps(payload), content_type="application/json"
    )

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["questionnaire"]["name"] == "My Q Copy"
    assert data["questionnaire"]["status"] == Questionnaire.QuestionnaireStatus.DRAFT
    # A new OQ was created
    assert data["id"] != str(oq.pk)
    new_oq = OrganizationQuestionnaire.objects.get(pk=data["id"])
    assert new_oq.organization_id == organization.pk


def test_duplicate_questionnaire_by_staff_with_permission(
    organization: Organization,
    organization_staff_user: "object",
    organization_staff_client: Client,
    staff_member: OrganizationStaff,
) -> None:
    """Staff with create_questionnaire permission can duplicate."""
    perms = staff_member.permissions
    perms["default"]["create_questionnaire"] = True
    staff_member.permissions = perms
    staff_member.save()

    oq = _make_org_questionnaire(organization)
    payload = {"name": "Staff Copy"}

    response = organization_staff_client.post(
        _duplicate_url(oq), data=orjson.dumps(payload), content_type="application/json"
    )

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["questionnaire"]["name"] == "Staff Copy"


def test_duplicate_questionnaire_copy_associations_false(
    organization: Organization,
    organization_owner_client: Client,
    event: "object",
) -> None:
    """copy_associations=false (default) produces an unattached copy."""
    oq = _make_org_questionnaire(organization)
    from events.models import Event

    if isinstance(event, Event):
        oq.events.add(event)

    payload = {"name": "No Links", "copy_associations": False}

    response = organization_owner_client.post(
        _duplicate_url(oq), data=orjson.dumps(payload), content_type="application/json"
    )

    assert response.status_code == 200, response.content
    data = response.json()
    assert data["events"] == []
    assert data["event_series"] == []


def test_duplicate_questionnaire_copy_associations_true(
    organization: Organization,
    organization_owner_client: Client,
    event: "object",
) -> None:
    """copy_associations=true propagates event/series links to the copy."""
    from events.models import Event

    oq = _make_org_questionnaire(organization)
    if isinstance(event, Event):
        oq.events.add(event)

    payload = {"name": "With Links", "copy_associations": True}

    response = organization_owner_client.post(
        _duplicate_url(oq), data=orjson.dumps(payload), content_type="application/json"
    )

    assert response.status_code == 200, response.content
    data = response.json()
    # The copy carries the event link
    assert len(data["events"]) == 1


# ---------------------------------------------------------------------------
# Permission / auth failures
# ---------------------------------------------------------------------------


def test_duplicate_questionnaire_by_staff_without_permission(
    organization: Organization,
    organization_staff_client: Client,
    staff_member: OrganizationStaff,
) -> None:
    """Staff without create_questionnaire permission gets 403."""
    # Ensure create_questionnaire is False
    perms = PermissionsSchema(default=PermissionMap(create_questionnaire=False)).model_dump(mode="json")
    staff_member.permissions = perms
    staff_member.save()

    oq = _make_org_questionnaire(organization)
    payload = {"name": "Should Fail"}

    response = organization_staff_client.post(
        _duplicate_url(oq), data=orjson.dumps(payload), content_type="application/json"
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    "client_fixture,expected_status",
    [
        # member_client: org member, OQ is visible via for_user → but no create_questionnaire perm → 403
        ("member_client", 403),
        # nonmember_client: no org relationship, OQ is invisible via for_user queryset → 404
        ("nonmember_client", 404),
        # anonymous: no auth → 401 before any lookup
        ("client", 401),
    ],
)
def test_duplicate_questionnaire_unauthorized(
    request: pytest.FixtureRequest,
    client_fixture: str,
    expected_status: int,
    organization: Organization,
    organization_owner_client: Client,
) -> None:
    """Members, non-members, and anonymous users cannot duplicate."""
    oq = _make_org_questionnaire(organization)
    client: Client = request.getfixturevalue(client_fixture)
    payload = {"name": "Unauthorized"}

    response = client.post(_duplicate_url(oq), data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == expected_status


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------


def test_duplicate_questionnaire_not_found(
    organization_owner_client: Client,
) -> None:
    """Duplicating a non-existent OQ returns 404."""
    from uuid import uuid4

    fake_id = uuid4()
    url = reverse("api:duplicate_org_questionnaire", kwargs={"org_questionnaire_id": fake_id})
    payload = {"name": "Ghost"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404

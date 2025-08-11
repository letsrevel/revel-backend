import pytest
from django.shortcuts import reverse  # type: ignore[attr-defined]
from django.test.client import Client

from common.models import Tag

pytestmark = pytest.mark.django_db


def test_list_tags_endpoint(client: Client) -> None:
    """
    Tests that the /api/tags/ endpoint correctly lists and searches for tags.
    """
    # Arrange: Create some tags
    Tag.objects.create(name="Community", description="Events for the community.")
    Tag.objects.create(name="Tech Talk", description="Discussions about technology.")
    Tag.objects.create(name="Workshop", description="Hands-on learning sessions.")

    # Act 1: List all tags
    list_url = reverse("api:list_tags")
    response_list = client.get(list_url)
    data_list = response_list.json()

    # Assert 1
    assert response_list.status_code == 200
    assert data_list["count"] == 3
    assert len(data_list["results"]) == 3
    tag_names = {tag["name"] for tag in data_list["results"]}
    assert {"Community", "Tech Talk", "Workshop"} == tag_names

    # Act 2: Search for a specific tag
    search_url = f"{list_url}?search=Tech"
    response_search = client.get(search_url)
    data_search = response_search.json()

    # Assert 2
    assert response_search.status_code == 200
    assert data_search["count"] == 1
    assert data_search["results"][0]["name"] == "Tech Talk"

    # Act 3: Search with no results
    no_result_url = f"{list_url}?search=NonExistent"
    response_no_result = client.get(no_result_url)
    data_no_result = response_no_result.json()

    # Assert 3
    assert response_no_result.status_code == 200
    assert data_no_result["count"] == 0

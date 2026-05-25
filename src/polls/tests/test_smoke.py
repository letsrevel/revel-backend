import pytest
from django.apps import apps


@pytest.mark.django_db
def test_polls_app_is_registered() -> None:
    """The polls app must be installed and discoverable."""
    config = apps.get_app_config("polls")
    assert config.name == "polls"


def test_polls_label_is_unique() -> None:
    """The polls app label must not collide with other apps."""
    labels = [config.label for config in apps.get_app_configs()]
    assert labels.count("polls") == 1

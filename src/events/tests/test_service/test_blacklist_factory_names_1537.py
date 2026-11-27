"""Factory users never match a name-based blacklist entry by chance (#1537).

The matcher compares the first, last, preferred and full name with a fuzzy ratio of 85, the last name alone too.
Factory users had Faker names, so a common surname matched an entry such as "John Smith" now and then.
"""

import pytest

from conftest import RevelUserFactory
from events.models import Blacklist
from events.service.blacklist_service import get_fuzzy_match_score

pytestmark = pytest.mark.django_db

COMMON_NAMES = [("John", "Smith"), ("Maria", "Garcia"), ("Anna", "Müller"), ("Luca", "Rossi"), ("Wei", "Wang")]


def test_factory_users_match_no_common_name(revel_user_factory: RevelUserFactory) -> None:
    entries = [Blacklist(first_name=first, last_name=last) for first, last in COMMON_NAMES]
    users = [revel_user_factory() for _ in range(30)]

    assert [(u.pk, e.last_name) for u in users for e in entries if get_fuzzy_match_score(u, e)] == []


def test_factory_users_match_no_other_factory_user(revel_user_factory: RevelUserFactory) -> None:
    users = [revel_user_factory() for _ in range(20)]
    entries = [Blacklist(first_name=u.first_name, last_name=u.last_name) for u in users]

    matches = [
        (i, j) for i, u in enumerate(users) for j, e in enumerate(entries) if i != j and get_fuzzy_match_score(u, e)
    ]

    assert matches == []


def test_a_name_that_a_test_sets_is_kept(revel_user_factory: RevelUserFactory) -> None:
    user = revel_user_factory(first_name="John", last_name="")

    assert (user.first_name, user.last_name) == ("John", "")

"""Tests for django-solo singleton caching (SOLO_CACHE).

Regression coverage for the N+1 where every ``SingletonModel.get_solo()`` ran a
``get_or_create()``: notification-dispatch loops issued one identical
``SELECT ... FROM common_sitesettings`` per recipient (observed as 100+ duplicate
queries in a single event-status-change request). With ``SOLO_CACHE`` enabled the
singleton is served from the cache after the first read.
"""

import typing as t

import pytest

from common.models import SiteSettings


@pytest.mark.django_db
def test_get_solo_is_cached_after_first_call(django_assert_num_queries: t.Any) -> None:
    """Repeated get_solo() reads must not touch the DB once the singleton is cached."""
    # Prime the cache. The first call may SELECT (+ INSERT) the singleton row.
    SiteSettings.get_solo()

    with django_assert_num_queries(0):
        for _ in range(10):
            SiteSettings.get_solo()


@pytest.mark.django_db
def test_get_solo_cache_refreshes_on_save(django_assert_num_queries: t.Any) -> None:
    """Saving the singleton refreshes the cache: subsequent reads are correct and query-free."""
    site_settings = SiteSettings.get_solo()
    site_settings.notify_user_joined = True
    site_settings.save()

    with django_assert_num_queries(0):
        refreshed = SiteSettings.get_solo()

    assert refreshed.notify_user_joined is True

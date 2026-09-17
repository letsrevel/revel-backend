"""Guard for the hand-added ``run_before`` in ``oauth/migrations/0001_initial.py``."""

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader


@pytest.mark.django_db
def test_oauth_initial_runs_before_dot_initial() -> None:
    """DOT's 0001 has no dependency on the swapped Application model; our run_before supplies it (spec §6.4)."""
    plan = MigrationLoader(connection).graph.forwards_plan(("oauth2_provider", "0001_initial"))
    assert plan.index(("oauth", "0001_initial")) < plan.index(("oauth2_provider", "0001_initial"))

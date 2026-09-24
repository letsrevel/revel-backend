"""Guard for the hand-added ``run_before`` in ``oauth/migrations/0001_initial.py``."""

import pytest
from django.db import connection
from django.db.migrations.loader import MigrationLoader


@pytest.mark.django_db
def test_oauth_initial_runs_before_dot_initial() -> None:
    """DOT's 0001 has no dependency on the swapped Application model; our run_before supplies it (spec §6.4)."""
    plan = MigrationLoader(connection).graph.forwards_plan(("oauth2_provider", "0001_initial"))
    assert plan.index(("oauth", "0001_initial")) < plan.index(("oauth2_provider", "0001_initial"))


@pytest.mark.django_db
def test_dot_token_tables_take_the_uuid_application_key() -> None:
    """``OAuthApplication.id`` is a UUID, and DOT's swappable FKs must be generated to match (R-82).

    This is the invariant the UUID primary key rests on: DOT's own migrations build the token
    tables' ``application_id`` columns from the swapped model's key, so if a regeneration ever
    dropped the override the columns would silently become ``bigint`` again — and the shared
    upload service (``FileUploadAudit.instance_pk`` is a ``UUIDField``) would break with it.
    The assertion runs against the real, migrated-from-scratch test database.
    """
    tables = [
        "oauth2_provider_accesstoken",
        "oauth2_provider_refreshtoken",
        "oauth2_provider_idtoken",
        "oauth2_provider_grant",
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_name, data_type
            FROM information_schema.columns
            WHERE column_name = 'application_id' AND table_name = ANY(%s)
            """,
            [tables],
        )
        found = dict(cursor.fetchall())
    assert found == dict.fromkeys(tables, "uuid"), found

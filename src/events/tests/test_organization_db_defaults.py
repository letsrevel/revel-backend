"""Rolling-deploy safety: white-label columns carry a database default (#1148 review)."""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("column", "expected"),
    [("custom_domain_enabled", "false"), ("branding", "'{}'::jsonb")],
)
def test_not_null_white_label_columns_have_a_database_default(column: str, expected: str) -> None:
    """A pod still on the previous release inserts organizations without these columns."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'events_organization' AND column_name = %s",
            [column],
        )
        default, nullable = cursor.fetchone()

    assert nullable == "NO"
    assert default == expected

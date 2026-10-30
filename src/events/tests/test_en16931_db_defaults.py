"""Rolling-deploy safety: the NOT NULL columns #1234 added carry a database default.

``migrate`` runs in the new web container while the previous release's web and Celery pods keep
inserting rows without these columns (``infra/deploy-rollout.sh``); without a DB default those
INSERTs hit NOT NULL during the rollout window.
"""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        ("events_attendeeinvoice", "totals_method", "'legacy'::character varying"),
        ("events_attendeeinvoice", "rounding_amount", "0"),
        ("events_attendeeinvoice", "vat_exemptions", "'{}'::jsonb"),
        ("events_attendeeinvoice", "payment_means_code", "''::character varying"),
        ("events_attendeeinvoice", "payment_method_label", "''::character varying"),
        ("events_attendeeinvoice", "card_last4", "''::character varying"),
        ("events_attendeeinvoice", "seller_country_code", "''::character varying"),
        ("events_attendeeinvoice", "buyer_city", "''::character varying"),
        ("events_attendeeinvoicecreditnote", "totals_method", "'legacy'::character varying"),
        ("events_attendeeinvoicecreditnote", "rounding_amount", "0"),
        ("events_organization", "billing_city", "''::character varying"),
        ("events_organization", "vat_exemption_reason", "''::character varying"),
        ("events_tickettier", "vat_exemption_reason", "''::character varying"),
        ("accounts_userbillingprofile", "billing_country_code", "''::character varying"),
    ],
)
def test_not_null_en16931_columns_have_a_database_default(table: str, column: str, expected: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            [table, column],
        )
        default, nullable = cursor.fetchone()

    assert nullable == "NO"
    assert default == expected

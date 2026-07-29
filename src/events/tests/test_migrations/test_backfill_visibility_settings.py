"""Unit tests for the 0105 backfill helpers (#793).

``django-test-migrations`` is not an installed dependency, and the columns this
migration reads are dropped by 0106 — so the live app registry cannot replay it
(the pattern used by ``test_backfill_reservation_id.py``). Instead the row-level
logic lives in two pure functions, tested exhaustively here; the plumbing that
walks the table is verified by hand against a seeded database before merge.
"""

import importlib
import typing as t

from events.utils.visibility_settings import ResourceVisibility, validate_visibility_settings

_migration = importlib.import_module("events.migrations.0105_backfill_visibility_settings_from_columns")
merge_into_blob = _migration.merge_into_blob
split_out_of_blob = _migration.split_out_of_blob


class TestMergeIntoBlob:
    def test_empty_blob_gains_both_keys(self) -> None:
        """The common case: an event untouched since #792 shipped."""
        assert merge_into_blob({}, "attendees-only", True) == {
            "address_visibility": "attendees-only",
            "show_pronoun_distribution": True,
        }

    def test_none_blob_is_treated_as_empty(self) -> None:
        """Defensive: the column is NOT NULL, but a None must not crash the deploy."""
        assert merge_into_blob(None, "public", False) == {
            "address_visibility": "public",
            "show_pronoun_distribution": False,
        }

    def test_existing_toggles_survive(self) -> None:
        """The #792 toggles must not be clobbered — that is the whole risk here."""
        stored = {"show_capacity": False, "show_attendee_list": False}
        assert merge_into_blob(stored, "staff-only", False) == {
            "show_capacity": False,
            "show_attendee_list": False,
            "address_visibility": "staff-only",
            "show_pronoun_distribution": False,
        }

    def test_does_not_mutate_the_input(self) -> None:
        """bulk_update batches share nothing; an in-place merge would be a silent aliasing bug."""
        stored = {"show_capacity": False}
        merge_into_blob(stored, "private", True)
        assert stored == {"show_capacity": False}

    def test_default_column_values_are_written_explicitly(self) -> None:
        """Matches what ``create_event`` already stores for new events: the full blob."""
        assert merge_into_blob({}, "public", False) == {
            "address_visibility": "public",
            "show_pronoun_distribution": False,
        }

    def test_column_wins_when_blob_already_has_the_key(self) -> None:
        """Pins the clobber this function does NOT protect against on its own.

        ``forwards`` never calls this on such a row — it excludes rows already
        carrying ``address_visibility`` via ``.exclude(...has_key=...)``, because
        an event created by the *new* code mid-migration has the real value in
        its blob and only a defaulted value in the column. This test documents
        why that predicate-level skip is load-bearing: if it were ever bypassed,
        the column would silently overwrite the blob's real value.
        """
        blob = {"address_visibility": "private", "show_pronoun_distribution": True}
        assert merge_into_blob(blob, "public", False) == {
            "address_visibility": "public",
            "show_pronoun_distribution": False,
        }


class TestSplitOutOfBlob:
    def test_recovers_values_and_strips_keys(self) -> None:
        """Reverse must strip: ``extra='forbid'`` makes a leftover key fatal on rollback."""
        blob = {"show_capacity": False, "address_visibility": "members-only", "show_pronoun_distribution": True}
        assert split_out_of_blob(blob) == ("members-only", True, {"show_capacity": False})

    def test_missing_keys_fall_back_to_column_defaults(self) -> None:
        """A row written between the deploy and the migration has no keys yet."""
        assert split_out_of_blob({}) == ("public", False, {})

    def test_none_blob_is_treated_as_empty(self) -> None:
        assert split_out_of_blob(None) == ("public", False, {})

    def test_does_not_mutate_the_input(self) -> None:
        blob = {"address_visibility": "private", "show_pronoun_distribution": True}
        split_out_of_blob(blob)
        assert blob == {"address_visibility": "private", "show_pronoun_distribution": True}

    def test_stripped_blob_validates_against_the_pre_793_model(self) -> None:
        """The rollback trap: a leftover key would raise for every migrated event."""
        blob = merge_into_blob({"show_capacity": False}, "staff-only", True)
        _, _, remainder = split_out_of_blob(blob)
        assert set(remainder) <= {"show_attendee_count", "show_capacity", "show_attendee_list"}


class TestRoundTrip:
    def test_forward_then_reverse_is_identity(self) -> None:
        """Deploy, roll back: the organizer's configuration comes out unchanged."""
        cases: list[tuple[dict[str, t.Any], str, bool]] = [
            ({}, "public", False),
            ({}, "attendees-only", True),
            ({"show_capacity": False}, "staff-only", False),
            ({"show_attendee_count": False, "show_attendee_list": False}, "members-only", True),
        ]
        for blob, address_visibility, show_pronouns in cases:
            merged = merge_into_blob(blob, address_visibility, show_pronouns)
            assert split_out_of_blob(merged) == (address_visibility, show_pronouns, blob)

    def test_every_enum_member_round_trips(self) -> None:
        """No visibility level is lost or coerced by the migration."""
        for member in ResourceVisibility:
            merged = merge_into_blob({}, member.value, False)
            assert validate_visibility_settings(merged).address_visibility == member
            assert split_out_of_blob(merged)[0] == member.value

"""Keep the ``PermissionKey`` literal in sync with ``PermissionMap`` (see #683).

mypy checks every ``OrganizationPermission("...")`` (and sibling) call site against
:data:`~events.models.PermissionKey`, but it cannot verify that the literal itself lists
exactly the ``PermissionMap`` fields. This test closes that gap: if a permission is added
to (or renamed on) ``PermissionMap`` without updating the literal, CI fails here — before
a bogus key can silently deny every non-owner staff member.
"""

import typing as t

from events.models import PermissionKey, PermissionMap


def test_permission_key_matches_permission_map_fields() -> None:
    """The PermissionKey literal must list exactly the PermissionMap fields."""
    literal_keys = set(t.get_args(PermissionKey))
    model_fields = set(PermissionMap.model_fields)
    assert literal_keys == model_fields, (
        "PermissionKey and PermissionMap have drifted. "
        f"Only in PermissionKey: {literal_keys - model_fields}. "
        f"Only in PermissionMap: {model_fields - literal_keys}."
    )

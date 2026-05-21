from events.models.organization import PermissionMap


def test_permission_map_has_manage_polls_default_false() -> None:
    """`manage_polls` defaults to False on PermissionMap."""
    pm = PermissionMap()
    assert pm.manage_polls is False


def test_permission_map_accepts_manage_polls_true() -> None:
    """`manage_polls` can be set to True."""
    pm = PermissionMap(manage_polls=True)
    assert pm.manage_polls is True


def test_permission_map_unknown_keys_pass_through_silently() -> None:
    """Legacy JSON without `manage_polls` deserializes with the new default."""
    pm = PermissionMap.model_validate({"manage_tickets": True})
    assert pm.manage_polls is False
    assert pm.manage_tickets is True

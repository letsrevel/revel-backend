"""The error-code enum lives in enums.py; schemas reference model enums, not Literal mirrors."""

import typing as t

from integrations import enums, schema
from integrations.models import EventLink, PlatformConnection


def test_enum_lives_in_enums_and_is_reexported() -> None:
    assert schema.IntegrationErrorCode is enums.IntegrationErrorCode
    assert enums.IntegrationErrorCode.PAUSE_FAILED.value == "pause_failed"


def test_schemas_reference_model_enums() -> None:
    status_annotation = schema.ConnectionSchema.model_fields["status"].annotation
    assert t.get_args(status_annotation) == (PlatformConnection.Status, type(None))
    assert schema.EventLinkSchema.model_fields["remote_status"].annotation is EventLink.RemoteStatus
    assert schema.EventLinkSchema.model_fields["sync_state"].annotation is EventLink.SyncState
    assert schema.EventLinkSchema.model_fields["origin"].annotation is EventLink.Origin


def test_models_do_not_import_schema() -> None:
    import integrations.models as models_module

    assert not any(getattr(v, "__module__", "") == "integrations.schema" for v in vars(models_module).values())

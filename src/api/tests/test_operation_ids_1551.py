"""OpenAPI operationIds are stable and unique (#1551).

ninja_extra adds a random 8-character suffix unless ``use_unique_op_id=False``; ``common.api_controller`` turns it
off, and ``dump_openapi`` refuses duplicate ids.
"""

import re
import typing as t

import pytest

from api.api import api
from api.management.commands.dump_openapi import DuplicateOperationIdError, assert_unique_operation_ids

pytestmark = pytest.mark.django_db


def _operation_ids() -> list[str]:
    api._schema_cache = {}
    schema = t.cast(dict[str, t.Any], api.get_openapi_schema())
    return [op["operationId"] for ops in schema["paths"].values() for op in ops.values() if "operationId" in op]


def test_no_operation_id_has_a_random_suffix() -> None:
    ids = _operation_ids()

    assert ids
    assert [i for i in ids if re.search(r"_[0-9a-f]{8}$", i)] == []


def test_operation_ids_are_unique() -> None:
    ids = _operation_ids()

    assert len(ids) == len(set(ids))


def test_the_guard_refuses_a_duplicate_operation_id() -> None:
    schema = {"paths": {"/a": {"get": {"operationId": "x_list"}}, "/b": {"post": {"operationId": "x_list"}}}}

    with pytest.raises(DuplicateOperationIdError, match="GET /a and POST /b"):
        assert_unique_operation_ids(schema)


def test_the_guard_accepts_unique_ids() -> None:
    assert_unique_operation_ids({"paths": {"/a": {"get": {"operationId": "a"}, "post": {"operationId": "b"}}}})


def test_dump_openapi_refuses_a_duplicate_operation_id(settings: t.Any, tmp_path: t.Any) -> None:
    """The command runs the guard, and it writes no artifact for a refused schema."""
    from unittest.mock import patch

    from django.core.management import call_command

    settings.BASE_DIR = tmp_path / "src"
    duplicate = {"paths": {"/a": {"get": {"operationId": "x_list"}}, "/b": {"post": {"operationId": "x_list"}}}}

    with (
        patch.object(api, "get_openapi_schema", return_value=duplicate),
        pytest.raises(DuplicateOperationIdError),
    ):
        call_command("dump_openapi")

    assert not (tmp_path / ".artifacts" / "openapi.json").exists()

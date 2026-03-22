"""Shared test utilities for VIES-related tests."""

import typing as t
from unittest.mock import MagicMock


def mock_vies_response(
    *,
    valid: bool = True,
    name: str = "ACME SRL",
    address: str = "VIA ROMA 1, 00100 ROMA RM",
    request_identifier: str = "WAPIAAAAYeBtPMia",
    status_code: int = 200,
) -> MagicMock:
    """Build a mock httpx.Response for VIES REST API calls."""
    data: dict[str, t.Any] = {
        "valid": valid,
        "name": name,
        "address": address,
        "requestIdentifier": request_identifier,
    }
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = data
    response.text = str(data)
    return response

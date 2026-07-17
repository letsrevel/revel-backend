"""Tests for seat hold acquisition/release and guest hold sessions."""


from events.service.guest_hold_session import issue_guest_hold_token, resolve_guest_session


def test_guest_token_roundtrip() -> None:
    session_id, token = issue_guest_hold_token()
    assert resolve_guest_session(token) == session_id


def test_guest_token_tamper_rejected() -> None:
    _, token = issue_guest_hold_token()
    assert resolve_guest_session(token[:-2] + "xx") is None
    assert resolve_guest_session(None) is None
    assert resolve_guest_session("") is None

"""Tests for the ``stripe-webhook-sync-1505`` post-deploy task (#1508)."""

import json
import typing as t
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import CommandError

from common import post_deploy
from common.models import PostDeployTaskRun
from common.post_deploy import PostDeployTask
from events import post_deploy as events_post_deploy
from events.management.commands.provision_stripe_webhooks import CONNECT_EVENTS, PLATFORM_EVENTS

TASK_ID = "stripe-webhook-sync-1505"


@pytest.fixture
def configured(settings: t.Any) -> None:
    """Settings that make the task runnable."""
    settings.STRIPE_SECRET_KEY = "sk_live_real"
    settings.STRIPE_WEBHOOK_SECRETS = ["whsec_real"]
    settings.BASE_URL = "https://api.example.com/"


def test_task_is_in_the_real_registry() -> None:
    task = next(task for task in post_deploy.registered() if task.id == TASK_ID)
    assert task.run is events_post_deploy._sync_webhooks


def test_no_skip_reason_when_configured(configured: None) -> None:
    assert events_post_deploy._sync_webhooks_skip_reason() is None


@pytest.mark.parametrize("key", ["", "sk_test_..."])
def test_skips_without_stripe_key(configured: None, settings: t.Any, key: str) -> None:
    settings.STRIPE_SECRET_KEY = key
    assert events_post_deploy._sync_webhooks_skip_reason() == "Stripe is not configured"


def test_skips_without_webhook_secret(configured: None, settings: t.Any) -> None:
    settings.STRIPE_WEBHOOK_SECRETS = ["whsec_..."]
    reason = events_post_deploy._sync_webhooks_skip_reason()
    assert reason is not None
    assert "no webhook secret" in reason


def test_skips_with_only_the_placeholder_webhook_secret(configured: None, settings: t.Any) -> None:
    settings.STRIPE_WEBHOOK_SECRETS = ["whsec_placeholder"]
    reason = events_post_deploy._sync_webhooks_skip_reason()
    assert reason is not None
    assert "no webhook secret" in reason


def test_skips_when_base_url_is_not_https(configured: None, settings: t.Any) -> None:
    settings.BASE_URL = "http://localhost:8000"
    assert events_post_deploy._sync_webhooks_skip_reason() == "BASE_URL is not https"


def test_body_calls_the_sync_command(configured: None) -> None:
    def fake_call_command(*args: t.Any, **kwargs: t.Any) -> None:
        kwargs["stdout"].write('{"dry_run": false}')

    with patch.object(events_post_deploy, "call_command", MagicMock(side_effect=fake_call_command)) as mock:
        output = events_post_deploy._sync_webhooks()
    assert output == '{"dry_run": false}'
    args, kwargs = mock.call_args
    assert args == ("provision_stripe_webhooks",)
    assert kwargs["url"] == "https://api.example.com/api/stripe/webhook"
    assert kwargs["sync"] is True
    assert kwargs["format"] == "json"


@pytest.mark.django_db
def test_command_error_is_recorded_as_failed(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    task = PostDeployTask(
        id=TASK_ID,
        release="3.0.1",
        description="d",
        run=events_post_deploy._sync_webhooks,
        skip_reason=events_post_deploy._sync_webhooks_skip_reason,
    )
    monkeypatch.setattr(post_deploy, "_REGISTRY", {TASK_ID: task})
    with patch.object(events_post_deploy, "call_command", side_effect=CommandError("no endpoints")):
        results = post_deploy.run_pending()
    assert results[0]["outcome"] == "failed"
    row = PostDeployTaskRun.objects.get(task_id=TASK_ID)
    assert row.status == PostDeployTaskRun.Status.FAILED
    assert "CommandError: no endpoints" in row.output


def test_real_sync_command_output_is_json(configured: None) -> None:
    """Run the real command (Stripe patched at the API boundary) and parse what the runner would store."""
    url = "https://api.example.com/api/stripe/webhook"
    old_platform = [e for e in PLATFORM_EVENTS if not e.startswith("charge.dispute.")]
    platform = SimpleNamespace(
        id="we_p",
        url=url,
        enabled_events=[*old_platform, "customer.created"],
        description="Revel platform events",
        status="enabled",
    )
    connect = SimpleNamespace(
        id="we_c", url=url, enabled_events=list(CONNECT_EVENTS), description="Revel Connect events", status="enabled"
    )
    with (
        patch("events.management.commands.provision_stripe_webhooks.stripe") as stripe_mock,
        patch("events.management.commands.provision_stripe_webhooks.configure_stripe"),
    ):
        stripe_mock.WebhookEndpoint.list.return_value.auto_paging_iter.return_value = iter([platform, connect])
        output = events_post_deploy._sync_webhooks()
    payload = json.loads(output)
    assert payload["dry_run"] is False
    assert payload["platform"]["id"] == "we_p"
    assert payload["platform"]["added"] == sorted(set(PLATFORM_EVENTS) - set(old_platform))
    assert payload["platform"]["removed"] == ["customer.created"]
    assert payload["connect"]["added"] == []
    assert payload["connect"]["removed"] == []

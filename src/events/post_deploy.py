"""Post-deploy tasks owned by the events app (#1508)."""

import io

from django.conf import settings
from django.core.management import call_command

from common.post_deploy import PostDeployTask, register

_PLACEHOLDER_SECRET_KEY = "sk_test_..."
_PLACEHOLDER_WEBHOOK_SECRET = "whsec_..."


def _webhook_url() -> str:
    return f"{settings.BASE_URL.rstrip('/')}/api/stripe/webhook"


def _sync_webhooks() -> str:
    """Sync the enabled event types of the Stripe webhook endpoints. Return the command's JSON output."""
    buf = io.StringIO()
    call_command("provision_stripe_webhooks", url=_webhook_url(), sync=True, format="json", stdout=buf)
    return buf.getvalue()


def _sync_webhooks_skip_reason() -> str | None:
    if not settings.STRIPE_SECRET_KEY or settings.STRIPE_SECRET_KEY == _PLACEHOLDER_SECRET_KEY:
        return "Stripe is not configured"
    if not any(s and s != _PLACEHOLDER_WEBHOOK_SECRET for s in settings.STRIPE_WEBHOOK_SECRETS):
        return "Stripe is not configured (no webhook secret; endpoints were never provisioned)"
    if not _webhook_url().startswith("https://"):
        return "BASE_URL is not https"
    return None


def register_post_deploy_tasks() -> None:
    """Register the events post-deploy tasks. Called from ``EventsConfig.ready``."""
    register(
        PostDeployTask(
            id="stripe-webhook-sync-1505",
            release="3.0.1",
            description="Sync Stripe webhook endpoints with the canonical event lists (refund.* events, #1505)",
            run=_sync_webhooks,
            skip_reason=_sync_webhooks_skip_reason,
        )
    )

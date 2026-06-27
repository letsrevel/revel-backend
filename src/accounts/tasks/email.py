"""Transactional account email-send task (verification, activation, password reset, email change, deletion).

A single template-driven Celery task — :func:`send_account_email` — renders and sends every
transactional account email. Each message type is described by an :data:`_CONFIGS` entry
(template base, frontend link path, and any required extra context), so adding a new message is a
one-line config addition rather than another near-identical wrapper task.

Every body is rendered with ``frontend_base_url`` in context and every link-bearing template uses
the same ``action_link`` key, so the config carries only what actually differs per message —
no per-template "does this one use the base URL / a link" flags.

Consolidated from eight per-message wrapper tasks (issue #608). The old task names
(``accounts.tasks.send_verification_email`` etc.) are **gone** — deploys must drain Celery
(``safe_reboot``) so no in-flight message references a removed name.
"""

import dataclasses
import enum

import structlog
from celery import shared_task
from django.template.loader import render_to_string

from common.models import SiteSettings
from common.tasks import send_email

logger = structlog.get_logger(__name__)


class AccountEmail(enum.StrEnum):
    """The transactional account email types dispatched via :func:`send_account_email`."""

    VERIFICATION = "verification"
    ACTIVATION = "activation"
    PASSWORD_RESET = "password_reset"
    CHANGE_CONFIRMATION = "change_confirmation"
    CHANGE_NOTICE = "change_notice"
    CHANGE_COMPLETED_OLD = "change_completed_old"
    CHANGE_COMPLETED_NEW = "change_completed_new"
    DELETION = "deletion"


@dataclasses.dataclass(frozen=True)
class _Config:
    """Static description of one transactional account email.

    Attributes:
        template_base: Stem under ``accounts/emails/`` — renders ``{base}_subject.txt``,
            ``{base}_body.txt`` and ``{base}_body.html``. The body templates receive
            ``frontend_base_url`` and, for link-bearing messages, ``action_link``.
        link_path: Frontend path (a ``{token}`` format string) appended to ``frontend_base_url``
            to build ``action_link``. ``None`` for the informational emails that carry no link.
        required_context_keys: Caller-supplied context keys the templates require — validated
            up front so a bad dispatch fails fast instead of rendering a partial email.
    """

    template_base: str
    link_path: str | None = None
    required_context_keys: tuple[str, ...] = ()


_CONFIGS: dict[AccountEmail, _Config] = {
    AccountEmail.VERIFICATION: _Config(
        template_base="email_verification",
        link_path="/login/confirm-email?token={token}",
    ),
    AccountEmail.ACTIVATION: _Config(
        template_base="account_activation",
        link_path="/login/reset-password?token={token}",
    ),
    AccountEmail.PASSWORD_RESET: _Config(
        template_base="password_reset",
        link_path="/login/reset-password?token={token}",
    ),
    AccountEmail.CHANGE_CONFIRMATION: _Config(
        template_base="email_change_confirmation",
        link_path="/account/confirm-email-change?token={token}",
    ),
    AccountEmail.CHANGE_NOTICE: _Config(
        template_base="email_change_notice",
        required_context_keys=("masked_new_email",),
    ),
    AccountEmail.CHANGE_COMPLETED_OLD: _Config(
        template_base="email_change_completed_old",
        required_context_keys=("old_email", "new_email"),
    ),
    AccountEmail.CHANGE_COMPLETED_NEW: _Config(
        template_base="email_change_completed_new",
        required_context_keys=("old_email", "new_email"),
    ),
    AccountEmail.DELETION: _Config(
        template_base="account_delete",
        link_path="/account/confirm-deletion?token={token}",
    ),
}


@shared_task(name="accounts.tasks.send_account_email")
def send_account_email(
    email_type: str,
    to: str,
    *,
    token: str | None = None,
    context: dict[str, str] | None = None,
) -> None:
    """Render and send a transactional account email.

    Args:
        email_type: An :class:`AccountEmail` value selecting the message and its template set.
        to: Recipient email address.
        token: Single-use token for the link-bearing emails (verification, activation,
            password reset, email-change confirmation, deletion). ``None`` for the
            informational email-change emails.
        context: Extra template context required by some message types — e.g.
            ``{"masked_new_email": ...}`` for the change notice, or
            ``{"old_email": ..., "new_email": ...}`` for the change-completed emails.

    Raises:
        ValueError: If a link-bearing email type is dispatched without a token, or a message
            type is dispatched without its required context keys.
    """
    config = _CONFIGS[AccountEmail(email_type)]
    logger.info("account_email_sending", email_type=email_type, to=to)

    caller_context = context or {}
    missing_keys = [key for key in config.required_context_keys if key not in caller_context]
    if missing_keys:
        raise ValueError(f"{email_type} email requires context keys: {', '.join(missing_keys)}")

    site_settings = SiteSettings.get_solo()
    body_context: dict[str, str] = {"frontend_base_url": site_settings.frontend_base_url, **caller_context}
    if config.link_path is not None:
        if token is None:
            raise ValueError(f"{email_type} email requires a token")
        body_context["action_link"] = site_settings.frontend_base_url + config.link_path.format(token=token)

    subject = str(render_to_string(f"accounts/emails/{config.template_base}_subject.txt"))
    body = render_to_string(f"accounts/emails/{config.template_base}_body.txt", body_context)
    html_body = render_to_string(f"accounts/emails/{config.template_base}_body.html", body_context)
    send_email(to=to, subject=subject, body=body, html_body=html_body)

    logger.info("account_email_sent", email_type=email_type, to=to)

"""Transactional account email-send task (verification, activation, password reset, email change, deletion).

A single template-driven Celery task — :func:`send_account_email` — renders and sends every
transactional account email. Each message type is described by an :data:`_CONFIGS` entry
(template base, frontend link path, and which extra context the templates expect), so adding a
new message is a one-line config addition rather than another near-identical wrapper task.

Consolidated from eight per-message wrapper tasks (issue #608). The old task names
(``accounts.tasks.send_verification_email`` etc.) are **gone** — deploys must drain Celery
(``safe_reboot``) so no in-flight message references a removed name.
"""

import dataclasses
import enum

import structlog
from celery import shared_task
from django.contrib.sites.models import Site
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
            ``{base}_body.txt`` and ``{base}_body.html``.
        link_path: Frontend path (a ``{token}`` format string) appended to
            ``frontend_base_url`` to build the action link. ``None`` for the informational
            emails that carry no link.
        link_context_key: Context key the body templates use for the built link.
        required_context_keys: Caller-supplied context keys the templates require — validated
            up front so a bad dispatch fails fast instead of rendering a partial email.
        include_frontend_base_url: Whether the body templates reference ``frontend_base_url``.
        subject_includes_site_name: Whether the subject template references ``site_name``
            (taken from the current ``Site``).
    """

    template_base: str
    link_path: str | None = None
    link_context_key: str | None = None
    required_context_keys: tuple[str, ...] = ()
    include_frontend_base_url: bool = False
    subject_includes_site_name: bool = False


_CONFIGS: dict[AccountEmail, _Config] = {
    AccountEmail.VERIFICATION: _Config(
        template_base="email_verification",
        link_path="/login/confirm-email?token={token}",
        link_context_key="verification_link",
    ),
    AccountEmail.ACTIVATION: _Config(
        template_base="account_activation",
        link_path="/login/reset-password?token={token}",
        link_context_key="activation_link",
    ),
    AccountEmail.PASSWORD_RESET: _Config(
        template_base="password_reset",
        link_path="/login/reset-password?token={token}",
        link_context_key="password_reset_link",
    ),
    AccountEmail.CHANGE_CONFIRMATION: _Config(
        template_base="email_change_confirmation",
        link_path="/account/confirm-email-change?token={token}",
        link_context_key="confirmation_link",
        include_frontend_base_url=True,
    ),
    AccountEmail.CHANGE_NOTICE: _Config(
        template_base="email_change_notice",
        required_context_keys=("masked_new_email",),
        include_frontend_base_url=True,
    ),
    AccountEmail.CHANGE_COMPLETED_OLD: _Config(
        template_base="email_change_completed_old",
        required_context_keys=("old_email", "new_email"),
        include_frontend_base_url=True,
    ),
    AccountEmail.CHANGE_COMPLETED_NEW: _Config(
        template_base="email_change_completed_new",
        required_context_keys=("old_email", "new_email"),
        include_frontend_base_url=True,
    ),
    AccountEmail.DELETION: _Config(
        template_base="account_delete",
        link_path="/account/confirm-deletion?token={token}",
        link_context_key="account_deletion_link",
        include_frontend_base_url=True,
        subject_includes_site_name=True,
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

    site_settings = SiteSettings.get_solo()
    body_context: dict[str, str] = dict(context or {})
    missing_keys = [key for key in config.required_context_keys if key not in body_context]
    if missing_keys:
        raise ValueError(f"{email_type} email requires context keys: {', '.join(missing_keys)}")
    if config.link_path is not None:
        if token is None:
            raise ValueError(f"{email_type} email requires a token")
        assert config.link_context_key is not None
        body_context[config.link_context_key] = site_settings.frontend_base_url + config.link_path.format(token=token)
    if config.include_frontend_base_url:
        body_context["frontend_base_url"] = site_settings.frontend_base_url

    subject_context = {"site_name": Site.objects.get_current().name} if config.subject_includes_site_name else {}
    subject = str(render_to_string(f"accounts/emails/{config.template_base}_subject.txt", subject_context))
    body = render_to_string(f"accounts/emails/{config.template_base}_body.txt", body_context)
    html_body = render_to_string(f"accounts/emails/{config.template_base}_body.html", body_context)
    send_email(to=to, subject=subject, body=body, html_body=html_body)

    logger.info("account_email_sent", email_type=email_type, to=to)

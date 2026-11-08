"""Celery tasks for the membership register: invitation emails and auto-linking (#1319)."""

from uuid import UUID

import structlog
from celery import shared_task
from django.template.loader import render_to_string
from django.utils import translation
from django.utils.translation import gettext as _

from common.tasks import send_email
from events.models import MembershipRecord
from events.service import member_register_linking
from events.tasks.attendees import _guest_language, _tenant_sender
from events.utils.frontend_urls import frontend_base_url_for

logger = structlog.get_logger(__name__)


@shared_task(name="events.send_member_register_invite")
def send_member_register_invite(record_id: str) -> None:
    """Email the claim link of an account-less register record.

    A direct templated email (like guest confirmations), not a ``NotificationType``: the notification system
    needs a recipient account, and handing the claim link to whoever registered the address would defeat it.
    The current ``invite_token`` is read from the record (it never travels through the broker), so a resend
    simply makes the latest link win. Skipped when the record is gone, already linked or holds no token.
    """
    record = MembershipRecord.objects.select_related("organization").filter(pk=record_id).first()
    token = record.invite_token if record else None
    if record is None or record.user_id is not None or not record.email or not token:
        logger.info("member_register_invite_skipped", record_id=record_id)
        return
    org = record.organization
    ctx = {
        "organization_name": org.name,
        "full_name": record.full_name,
        "claim_link": frontend_base_url_for(org) + f"/member-invite/{token}",
    }
    with translation.override(_guest_language(record.email)):
        subject = _("%(organization)s invites you to link your membership") % {"organization": org.name}
        body = render_to_string("events/emails/member_register_invite_body.txt", ctx)
        html_body = render_to_string("events/emails/member_register_invite_body.html", ctx)
    send_email(
        to=record.email,
        subject=subject,
        body=body,
        html_body=html_body,
        **_tenant_sender(org, "member_register_invite"),
    )
    logger.info("member_register_invite_sent", record_id=record_id)


@shared_task(name="events.auto_link_member_records")
def auto_link_member_records(user_id: str) -> None:
    """Link register records matching the user's verified email (see ``auto_link_records_for_user``)."""
    member_register_linking.auto_link_records_for_user(UUID(user_id))

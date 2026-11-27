"""Receivers for RevelUser and its preferences."""

import typing as t

import structlog
from django.db import transaction
from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from accounts.models import RevelUser
from events.models import (
    EventInvitation,
    GeneralUserPreferences,
    MembershipRecord,
    PendingEventInvitation,
)
from events.service.blacklist_retention import detach_blacklist_entries_of_account
from events.service.blacklist_service import (
    link_blacklist_entries_for_user,
)
from events.service.user_preferences_service import trigger_visibility_flags_for_user
from events.tasks.member_register import auto_link_member_records
from events.utils import get_invitation_message

# The logger name of the module before the split, so the log names stay the same.
logger = structlog.get_logger("events.signals")


@receiver(post_save, sender=RevelUser)
def handle_user_creation(sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: t.Any) -> None:
    """Creates GeneralUserPreferences, links blacklist entries, and processes pending invitations."""
    if not created:
        return
    logger.info("revel_user_created", user_id=str(instance.id))
    GeneralUserPreferences.objects.create(user=instance)

    # Link any existing blacklist entries that match this user's identifiers
    if linked_count := link_blacklist_entries_for_user(instance):
        logger.info("blacklist_entries_linked", user_id=str(instance.id), count=linked_count)

    # Convert any pending invitations for this email to real invitations
    pending_invitations = PendingEventInvitation.objects.prefetch_related("tiers").filter(email__iexact=instance.email)

    if pending_invitations.exists():
        logger.info(
            "converting_pending_invitations",
            user_id=str(instance.id),
            count=pending_invitations.count(),
        )

        with transaction.atomic():
            for pending in pending_invitations:
                # Preserve any existing custom message from the pending invitation,
                # or generate a default one using the user's display name if none was set.
                custom_message = pending.custom_message
                if not custom_message:
                    custom_message = get_invitation_message(instance.get_display_name(), pending.event)

                # Create EventInvitation from PendingEventInvitation
                invitation = EventInvitation.objects.create(
                    event=pending.event,
                    user=instance,
                    waives_questionnaire=pending.waives_questionnaire,
                    waives_purchase=pending.waives_purchase,
                    overrides_max_attendees=pending.overrides_max_attendees,
                    waives_membership_required=pending.waives_membership_required,
                    waives_rsvp_deadline=pending.waives_rsvp_deadline,
                    waives_apply_deadline=pending.waives_apply_deadline,
                    custom_message=custom_message,
                )
                # Copy tier links from pending invitation
                pending_tiers = pending.tiers.all()
                if pending_tiers:
                    invitation.tiers.set(pending_tiers)
            pending_invitations.delete()


@receiver(post_save, sender=GeneralUserPreferences)
def handle_default_user_pref_save(
    sender: type[GeneralUserPreferences], instance: GeneralUserPreferences, **kwargs: t.Any
) -> None:
    """Trigger visibility task after user preferences is changed or created."""
    # Iterate over all future events the user is attending
    trigger_visibility_flags_for_user(instance.user_id)


_LINK_RELEVANT_USER_FIELDS = frozenset({"email", "email_verified", "guest"})


@receiver(pre_save, sender=RevelUser)
def stash_previous_link_state(
    sender: type[RevelUser], instance: RevelUser, update_fields: t.Iterable[str] | None = None, **kwargs: t.Any
) -> None:
    """Remember the old (email, email_verified, guest) so ``post_save`` can tell a link-relevant change (#1319)."""
    if instance._state.adding or (update_fields is not None and not _LINK_RELEVANT_USER_FIELDS & set(update_fields)):
        instance._register_link_prev = None  # type: ignore[attr-defined]
        return
    instance._register_link_prev = (  # type: ignore[attr-defined]
        RevelUser.objects.filter(pk=instance.pk).values("email", "email_verified", "guest").first()
    )


@receiver(post_save, sender=RevelUser)
def auto_link_member_records_on_verified_email(
    sender: type[RevelUser], instance: RevelUser, created: bool, **kwargs: t.Any
) -> None:
    """Link register records to a user who now holds a verified email (#1319).

    Fires when the user becomes verified, is created verified (OIDC/social), converts from guest or changes
    to a different verified email. Guests and unverified emails never link.
    """
    if instance.guest or not instance.email_verified or not instance.email:
        return
    prev: dict[str, t.Any] | None = getattr(instance, "_register_link_prev", None)
    if not created:
        if prev is None:  # save() touched none of the link-relevant fields
            return
        if prev["email_verified"] and not prev["guest"] and prev["email"].casefold() == instance.email.casefold():
            return
    if not MembershipRecord.objects.unlinked().filter(email__iexact=instance.email).exists():
        return
    transaction.on_commit(lambda: auto_link_member_records.delay(str(instance.id)))


@receiver(pre_delete, sender=RevelUser)
def keep_blacklist_entries_of_deleted_user(sender: type[RevelUser], instance: RevelUser, **kwargs: t.Any) -> None:
    """Keep the blacklist entries of an account in place, as unlinked entries, when the account is deleted.

    The receiver locks the account row (``FOR NO KEY UPDATE``) first. Then the function locks the
    ``Organization`` rows. The erase-in-place path already holds the account row.
    """
    fresh = RevelUser.objects.select_for_update(no_key=True).filter(pk=instance.pk).first()
    detach_blacklist_entries_of_account(fresh or instance)

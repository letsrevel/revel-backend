"""Every relation to RevelUser has a decided outcome when an account is erased in place (#1476).

The erasure walks the relations generically, so a new model with a user FK would silently get its
``on_delete`` outcome. This registry turns that into a decision: a new or changed relation fails here.
"""

import typing as t

from django.db.models import CASCADE, DO_NOTHING, PROTECT, SET_NULL
from django.db.models.deletion import get_candidate_relations_to_delete
from simple_history.models import registered_models

from accounts.models import RevelUser
from accounts.service import account_erasure
from events.models import HeldSeriesPass, Payment, Refund, Ticket
from events.service.account_deletion_service import KeptHoldings

DELETED = "deleted"  # CASCADE: the rows go, as with user.delete()
NULLED = "nulled"  # SET_NULL: the column is nulled, the row stays
BLOCKS = "blocks"  # PROTECT: rows stop the erasure (referrals are cleaned up first; an owner is refused)
PURGED = "purged"  # DO_NOTHING history mirror: gdpr.purge_user_history removes it after the erasure
KEPT_IF_HELD = "kept if held"  # the kept tickets, passes and their payments stay; the other rows go
KEPT = "kept"  # the row stays with the account

EXPECTED: dict[str, str] = {
    "accounts.DietaryRestriction.user": DELETED,
    "accounts.EmailVerificationReminderTracking.user": DELETED,
    "accounts.ExternalIdentity.user": DELETED,
    "accounts.GlobalBan.created_by": NULLED,
    "accounts.GlobalBan.user": NULLED,
    "accounts.ImpersonationLog.admin_user": DELETED,
    "accounts.ImpersonationLog.target_user": DELETED,
    "accounts.Referral.referred_user": NULLED,
    "accounts.Referral.referrer": BLOCKS,
    "accounts.ReferralApplication.decided_by": NULLED,
    "accounts.ReferralApplication.user": NULLED,
    "accounts.ReferralCode.user": BLOCKS,
    "accounts.RevelUser_groups.reveluser": DELETED,
    "accounts.RevelUser_user_permissions.reveluser": DELETED,
    "accounts.UserBillingProfile.user": DELETED,
    "accounts.UserDataExport.user": DELETED,
    "accounts.UserDietaryPreference.user": DELETED,
    "admin.LogEntry.user": DELETED,
    "common.FileExport.requested_by": DELETED,
    "common.HistoricalLegal.history_user": NULLED,
    "common.HistoricalSiteSettings.history_user": NULLED,
    "django_google_sso.GoogleSSOUser.user": DELETED,
    "events.Announcement.created_by": NULLED,
    "events.AttendeeInvoice.user": NULLED,
    "events.AttendeeVisibilityFlag.target": DELETED,
    "events.AttendeeVisibilityFlag.user": DELETED,
    "events.Blacklist.created_by": NULLED,
    "events.Blacklist.user": DELETED,
    "events.CustomerProfile.user": DELETED,
    "events.EventBookmark.user": DELETED,
    "events.EventInvitation.user": DELETED,
    "events.EventInvitationRequest.decided_by": NULLED,
    "events.EventInvitationRequest.user": DELETED,
    "events.EventQuestionnaireSubmission.user": DELETED,
    "events.EventRSVP.user": DELETED,
    "events.EventSeriesFollow.user": DELETED,
    "events.EventToken.issuer": DELETED,
    "events.EventWaitList.user": DELETED,
    "events.GeneralUserPreferences.user": DELETED,
    "events.HeldSeriesPass.user": KEPT_IF_HELD,
    "events.HistoricalCustomerProfile.history_user": NULLED,
    "events.HistoricalCustomerProfile.user": PURGED,
    "events.HistoricalMembershipPayment.history_user": NULLED,
    "events.HistoricalMembershipPayment.recorded_by": PURGED,
    "events.HistoricalMembershipSubscription.history_user": NULLED,
    "events.HistoricalMembershipSubscription.user": PURGED,
    "events.HistoricalMembershipSubscriptionPlan.history_user": NULLED,
    "events.MembershipPayment.recorded_by": NULLED,
    "events.MembershipRecord.created_by": NULLED,
    "events.MembershipRecord.duplicate_claimant": NULLED,
    "events.MembershipRecord.user": NULLED,
    "events.MembershipSubscription.user": DELETED,
    "events.Organization.owner": BLOCKS,
    "events.OrganizationContactMessage.sender": NULLED,
    "events.OrganizationFollow.user": DELETED,
    "events.OrganizationMember.user": DELETED,
    "events.OrganizationMembershipRequest.decided_by": NULLED,
    "events.OrganizationMembershipRequest.user": DELETED,
    "events.OrganizationStaff.user": DELETED,
    "events.OrganizationToken.invitee": DELETED,
    "events.OrganizationToken.issuer": DELETED,
    "events.OrganizationUserNote.updated_by": NULLED,
    "events.OrganizationUserNote.user": DELETED,
    "events.Payment.user": KEPT_IF_HELD,
    "events.PotluckItem.assignee": NULLED,
    "events.PotluckItem.created_by": NULLED,
    "events.Refund.initiated_by": NULLED,
    "events.SeatHold.user": DELETED,
    "events.SkippedFiscalDocument.resolved_by": NULLED,
    "events.SkippedFiscalDocument.user": NULLED,
    "events.Ticket.cancelled_by": NULLED,
    "events.Ticket.checked_in_by": NULLED,
    "events.Ticket.user": KEPT_IF_HELD,
    "events.WaitlistOffer.user": DELETED,
    "events.WhitelistRequest.decided_by": NULLED,
    "events.WhitelistRequest.user": DELETED,
    "notifications.Notification.user": DELETED,
    "notifications.NotificationPreference.user": KEPT,
    "oauth.OAuthApplication.user": DELETED,
    "oauth2_provider.AccessToken.user": DELETED,
    "oauth2_provider.DeviceGrant.user": DELETED,
    "oauth2_provider.Grant.user": DELETED,
    "oauth2_provider.IDToken.user": DELETED,
    "oauth2_provider.RefreshToken.user": DELETED,
    "questionnaires.QuestionnaireEvaluation.evaluator": NULLED,
    "questionnaires.QuestionnaireFile.uploader": DELETED,
    "questionnaires.QuestionnaireSubmission.user": DELETED,
    "telegram.TelegramUser.user": DELETED,
    "token_blacklist.OutstandingToken.user": NULLED,
}


def _outcome(relation: t.Any) -> str:
    """The outcome that ``account_erasure`` gives the rows of ``relation``."""
    on_delete = relation.field.remote_field.on_delete
    keep = account_erasure._kept_rows(
        relation.related_model, relation.field.name, KeptHoldings(frozenset(), frozenset())
    )
    if keep is not None:
        assert on_delete is CASCADE
        return KEPT if keep == {} else KEPT_IF_HELD
    return {CASCADE: DELETED, SET_NULL: NULLED, PROTECT: BLOCKS, DO_NOTHING: PURGED}[on_delete]


def test_every_relation_to_the_user_has_a_decided_outcome() -> None:
    """A new relation, or a changed ``on_delete``, must be added here with the outcome the erasure gives it."""
    relations: list[t.Any] = list(get_candidate_relations_to_delete(RevelUser._meta))  # type: ignore[arg-type]
    actual = {f"{rel.related_model._meta.label}.{rel.field.name}": _outcome(rel) for rel in relations}

    assert actual == EXPECTED


def test_no_kept_model_has_history() -> None:
    """The history purge drops the user's CASCADE history rows, which would also drop a kept row's history."""
    assert set(registered_models.values()).isdisjoint({Ticket, HeldSeriesPass, Payment, Refund})

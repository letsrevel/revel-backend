"""MembershipEligibilityService — runs the membership gate stack."""

from __future__ import annotations

import dataclasses
import functools
import typing as t
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import (
    MembershipSubscription,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
)
from notifications.enums import NotificationType
from notifications.signals import notification_requested
from questionnaires.models import QuestionnaireSubmission

from .enums import ReasonCode
from .gates import MEMBERSHIP_ELIGIBILITY_GATES, BaseMembershipEligibilityGate
from .resolvers import resolve_membership_questionnaire
from .types import MembershipEligibility

if t.TYPE_CHECKING:
    from events.models import Blacklist, WhitelistRequest


@dataclasses.dataclass(frozen=True)
class _BlacklistState:
    """Snapshot of a user's blacklist/whitelist standing for a given organization."""

    is_hard_blacklisted: bool
    fuzzy_matched_blacklist_entries: list["Blacklist"]
    is_whitelisted: bool
    whitelist_request: "WhitelistRequest | None"


class MembershipEligibilityService:
    """Per-request orchestrator that prefetches state and runs the gate chain."""

    def __init__(
        self,
        user: RevelUser,
        organization: Organization,
        tier: MembershipTier | None = None,
        plan: MembershipSubscriptionPlan | None = None,
    ) -> None:
        """Initialize the service, pre-fetching all required state.

        Subsequent gate checks rely on the prefetched attributes and the
        ``@cached_property`` blacklist state to avoid further database hits.
        Blacklist/whitelist data is computed lazily on first access — gates
        just read ``handler.is_hard_blacklisted`` etc. without priming.
        """
        self.user = user
        self.organization = organization
        self.tier = tier
        self.plan = plan

        # Owner/staff fast-path data.
        self.is_owner: bool = organization.owner_id == user.id
        self.staff_ids: set[uuid.UUID] = set(organization.staff_members.values_list("id", flat=True))

        # Memberships of this user in this org (any status), keyed by tier_id.
        self.user_memberships: dict[uuid.UUID | None, OrganizationMember] = {
            m.tier_id: m for m in OrganizationMember.objects.filter(organization=organization, user=user)
        }

        # Has user any non-terminal subscription in this org?
        self.has_non_terminal_subscription: bool = (
            MembershipSubscription.objects.filter(organization=organization, user=user)
            .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
            .exists()
        )

        # Existing applications for this (user, org), keyed by tier_id (None for tier-less).
        # Semantics: most-recent application per tier wins. The DISTINCT ON tier_id
        # bounds the result set to ONE row per tier (the newest, via the -created_at
        # secondary sort), so a long history of REJECTED/CANCELLED/COMPLETED rows
        # doesn't bloat the response. Gates that need terminal-state visibility
        # (notably ApplicationStatusGate) still see whatever the latest row is for
        # that tier — including REJECTED — which is the desired behavior.
        self.applications_by_tier: dict[uuid.UUID | None, OrganizationMembershipRequest] = {
            app.tier_id: app
            for app in (
                OrganizationMembershipRequest.objects.filter(organization=organization, user=user)
                .select_related("questionnaire_submission", "subscription")
                .order_by("tier_id", "-created_at")
                .distinct("tier_id")
            )
        }

        # Pre-resolve the applicable membership questionnaire and prefetch the
        # user's most-recent READY submission for it. Avoids an N+1 in the
        # questionnaire gate when this service is invoked per-row in list views.
        self.membership_questionnaire_oq = resolve_membership_questionnaire(organization, tier)
        self.latest_questionnaire_submission: QuestionnaireSubmission | None = None
        if self.membership_questionnaire_oq is not None:
            self.latest_questionnaire_submission = (
                QuestionnaireSubmission.objects.filter(
                    user=user,
                    questionnaire_id=self.membership_questionnaire_oq.questionnaire_id,
                    status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
                )
                .select_related("evaluation")
                .order_by("-submitted_at")
                .first()
            )

        self._gates: list[BaseMembershipEligibilityGate] = [gate(self) for gate in MEMBERSHIP_ELIGIBILITY_GATES]

    @property
    def current_application(self) -> OrganizationMembershipRequest | None:
        """The user's existing application for the target tier, if any."""
        if self.tier is None:
            return self.applications_by_tier.get(None)
        return self.applications_by_tier.get(self.tier.pk)

    @functools.cached_property
    def _blacklist_state(self) -> _BlacklistState:
        """Compute the user's blacklist/whitelist standing once, cache the result."""
        from events.service.blacklist_service import (
            check_user_hard_blacklisted,
            get_fuzzy_blacklist_matches,
        )
        from events.service.whitelist_service import (
            get_whitelist_request,
            is_user_whitelisted,
        )

        is_hard = check_user_hard_blacklisted(self.user, self.organization)
        if is_hard:
            return _BlacklistState(True, [], False, None)

        fuzzy_matches = get_fuzzy_blacklist_matches(self.user, self.organization)
        fuzzy_entries = [entry for entry, _score in fuzzy_matches]
        if not fuzzy_entries:
            return _BlacklistState(False, [], False, None)

        whitelisted = is_user_whitelisted(self.user, self.organization)
        request = None if whitelisted else get_whitelist_request(self.user, self.organization)
        return _BlacklistState(False, fuzzy_entries, whitelisted, request)

    @property
    def is_hard_blacklisted(self) -> bool:
        """True when the user is hard-blacklisted from the organization."""
        return self._blacklist_state.is_hard_blacklisted

    @property
    def fuzzy_matched_blacklist_entries(self) -> list["Blacklist"]:
        """Fuzzy-matched blacklist entries (empty when hard-blacklisted or no fuzzy hits)."""
        return self._blacklist_state.fuzzy_matched_blacklist_entries

    @property
    def is_whitelisted(self) -> bool:
        """True only when fuzzy-matched, not hard-blacklisted, and the user is on the whitelist."""
        return self._blacklist_state.is_whitelisted

    @property
    def whitelist_request(self) -> "WhitelistRequest | None":
        """Pending whitelist request, if any — None unless fuzzy-matched and not whitelisted."""
        return self._blacklist_state.whitelist_request

    def check_eligibility(self) -> MembershipEligibility:
        """Run the gate chain and return the first blocking/allowing verdict.

        If no gate short-circuits, returns ``allowed=True`` with no ``next_step``.
        """
        for gate in self._gates:
            result = gate.check()
            if result is not None:
                return result
        return MembershipEligibility(
            allowed=True,
            organization_id=self.organization.pk,
            tier_id=self.tier.pk if self.tier else None,
            plan_id=self.plan.pk if self.plan else None,
        )


#: Reasons that genuinely terminate an application — the user has no in-app
#: recourse on THIS row (they can still re-apply; see ApplicationStatusGate).
#: Everything else leaves the row PENDING.
TERMINAL_REJECTION_CODES: t.Final[frozenset[ReasonCode]] = frozenset(
    {
        ReasonCode.MEMBERSHIP_QUESTIONNAIRE_FAILED,
    }
)


def _notify_rejection(application: OrganizationMembershipRequest) -> None:
    """Queue the user-facing rejection notification for an auto-rejected application.

    Mirrors :func:`events.service.organization_service.reject_membership_request`
    so a system-driven rejection (terminal questionnaire failure observed on
    read) is not silent.
    """

    def send_rejection_notification() -> None:
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        notification_requested.send(
            sender=OrganizationMembershipRequest,
            user=application.user,
            notification_type=NotificationType.MEMBERSHIP_REQUEST_REJECTED,
            context={
                "organization_id": str(application.organization_id),
                "organization_name": application.organization.name,
                "frontend_url": f"{frontend_base_url}/organizations",
            },
        )

    transaction.on_commit(send_rejection_notification)


def advance_application(
    application: OrganizationMembershipRequest,
) -> tuple[OrganizationMembershipRequest, MembershipEligibility]:
    """Re-run eligibility for an existing application and advance its status.

    Called on every read so a PENDING application can transition to APPROVED or
    COMPLETED as soon as upstream conditions are met (questionnaire submitted,
    staff approved, etc.).

    Wrapped in a ``select_for_update`` transaction to make concurrent reads safe.
    The full prefetch runs inside the lock — that's ~4 short queries holding the
    OMR row lock, which we accept because the alternative (prefetching outside
    the lock then re-checking inside) opens a TOCTOU window.

    Returns a tuple of ``(application, eligibility)`` — the eligibility is the
    verdict that was just computed inside the gate run, so callers don't need to
    construct a second :class:`MembershipEligibilityService` to read it.
    For terminal applications (REJECTED / CANCELLED / COMPLETED) eligibility is
    a fresh verdict computed against the current state (still useful for the
    response payload), but no state mutation is performed.
    """
    with transaction.atomic():
        # Re-fetch under lock so we never miss a concurrent update.
        # ``of=("self",)`` so FOR UPDATE locks only the OMR row — the nullable FK joins
        # below would otherwise trigger "FOR UPDATE cannot be applied to the nullable
        # side of an outer join" on Postgres.
        application = (
            OrganizationMembershipRequest.objects.select_for_update(of=("self",))
            .select_related("organization", "tier", "plan", "subscription", "questionnaire_submission", "user")
            .get(pk=application.pk)
        )

        service = MembershipEligibilityService(
            user=application.user,
            organization=application.organization,
            tier=application.tier,
            plan=application.plan,
        )
        eligibility = service.check_eligibility()

        # Terminal states never advance — return the freshly-computed eligibility
        # for the response payload but do not mutate.
        if application.status in {
            OrganizationMembershipRequest.Status.REJECTED,
            OrganizationMembershipRequest.Status.CANCELLED,
            OrganizationMembershipRequest.Status.COMPLETED,
        }:
            return application, eligibility

        # Advance PENDING → APPROVED/COMPLETED when gates pass.
        if application.status == OrganizationMembershipRequest.Status.PENDING and eligibility.allowed:
            if application.plan_id is None and application.tier_id is not None:
                _complete_free_application(application)
            elif application.plan_id is not None:
                # Paid path: APPROVED awaiting /pay (Phase 2).
                application.status = OrganizationMembershipRequest.Status.APPROVED
                application.save(update_fields=["status", "updated_at"])
            # else: tier-less, plan-less application → stays PENDING until staff approves
            # (legacy /membership-requests path).

        # Auto-reject only on the explicit terminal allowlist. A missing
        # next_step is a UX hint ("nothing for you to do right now"), NOT a
        # terminal signal: WHITELIST_PENDING, ORG_NOT_VISIBLE, MEMBERSHIP_PAUSED,
        # DUPLICATE_ACTIVE_SUBSCRIPTION etc. are all recoverable upstream states
        # and must leave the row PENDING so it can resume when they clear.
        if (
            application.status == OrganizationMembershipRequest.Status.PENDING
            and not eligibility.allowed
            and eligibility.reason_code in TERMINAL_REJECTION_CODES
        ):
            application.status = OrganizationMembershipRequest.Status.REJECTED
            application.save(update_fields=["status", "updated_at"])
            _notify_rejection(application)

        return application, eligibility


def apply_for_membership(
    *,
    user: RevelUser,
    organization: Organization,
    tier: MembershipTier | None,
    notes: str | None,
    questionnaire_submission_id: uuid.UUID | None,
) -> tuple[OrganizationMembershipRequest, MembershipEligibility]:
    """Create (or refresh) the user's membership application and advance state.

    Idempotent: when a PENDING application already exists for (user, org, tier),
    re-runs the gate stack against the existing row and may advance its status.
    Attaches a fresh ``questionnaire_submission_id`` to an existing PENDING row
    on re-POST when one wasn't recorded yet.

    The partial unique index ``unique_pending_application_per_user_org_tier``
    (status=PENDING) is the source of truth for "one PENDING per tier" — a
    concurrent insert raising :class:`IntegrityError` is caught and the winner
    is fetched. Terminal rows (REJECTED/CANCELLED/COMPLETED) are excluded by
    the partial constraint, so users CAN submit a fresh /apply after rejection
    (a new PENDING row is created and runs the gates from scratch).

    Note: HTTP-shaped pre-gate hard-blocks (e.g. raising 403/404 to avoid org
    enumeration) live in the controller. By the time we reach this function we
    assume the user is permitted to materialize an OMR row.

    Raises:
        django.core.exceptions.ValidationError: When ``questionnaire_submission_id``
            does not reference a READY submission owned by *user* for the
            questionnaire that actually gates this (organization, tier).
    """
    _validate_questionnaire_submission(
        user=user,
        organization=organization,
        tier=tier,
        questionnaire_submission_id=questionnaire_submission_id,
    )
    with transaction.atomic():
        try:
            application, created = OrganizationMembershipRequest.objects.get_or_create(
                organization=organization,
                user=user,
                tier=tier,
                status=OrganizationMembershipRequest.Status.PENDING,
                defaults={
                    "message": notes or None,
                    "questionnaire_submission_id": questionnaire_submission_id,
                },
            )
        except IntegrityError:
            # The partial unique index caught a concurrent insert. Fetch the winner;
            # idempotent from the caller's POV.
            application = OrganizationMembershipRequest.objects.get(
                organization=organization,
                user=user,
                tier=tier,
                status=OrganizationMembershipRequest.Status.PENDING,
            )
            created = False
        # Allow attaching a submission on re-POST when the row didn't have one.
        if not created and questionnaire_submission_id and not application.questionnaire_submission_id:
            application.questionnaire_submission_id = questionnaire_submission_id
            application.save(update_fields=["questionnaire_submission", "updated_at"])

    return advance_application(application)


def _validate_questionnaire_submission(
    *,
    user: RevelUser,
    organization: Organization,
    tier: MembershipTier | None,
    questionnaire_submission_id: uuid.UUID | None,
) -> None:
    """Reject a ``questionnaire_submission_id`` that cannot satisfy this application's gate.

    The id is persisted onto the OMR row as audit evidence ("this submission
    satisfied the questionnaire gate"), so it must reference a READY submission
    that (a) belongs to the caller, (b) targets the questionnaire resolved for
    this (organization, tier). The ownership filter doubles as an
    anti-enumeration measure: a nonexistent id and another user's id fail
    identically.
    """
    if questionnaire_submission_id is None:
        return

    questionnaire_oq = resolve_membership_questionnaire(organization, tier)
    is_valid = (
        questionnaire_oq is not None
        and QuestionnaireSubmission.objects.filter(
            pk=questionnaire_submission_id,
            user=user,
            questionnaire_id=questionnaire_oq.questionnaire_id,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        ).exists()
    )
    if not is_valid:
        raise ValidationError(_("Invalid questionnaire submission."))


def _complete_free_application(application: OrganizationMembershipRequest) -> None:
    """Materialize the OrganizationMember for a free-path application and mark COMPLETED.

    Precondition: ``application.tier_id`` must be set. Callers are responsible
    for routing tier-less applications through the staff-approval path instead.
    The ValueError below is a backstop against misuse.

    Defense-in-depth: refuse to clobber an OrganizationMember row if the user
    has any non-terminal :class:`MembershipSubscription` for this organization,
    or if an existing membership at the target tier is PAUSED. The gate chain
    (:class:`AlreadyMemberGate`) is the primary line of defense; this guard
    catches any reordering / refactoring that bypasses it.
    """
    if application.tier_id is None:
        # Tier-less applications (legacy /membership-requests) must still go through staff approval —
        # they don't auto-complete. Caller should ensure this branch isn't reached without a tier.
        raise ValueError("Cannot auto-complete tier-less free application; staff must assign a tier.")

    if (
        MembershipSubscription.objects.filter(organization=application.organization, user=application.user)
        .exclude(status__in=MembershipSubscription.TERMINAL_STATUSES)
        .exists()
    ):
        # The user has a paid (or otherwise non-terminal) subscription. Free
        # completion would silently overwrite the paying membership — refuse.
        raise ValueError(
            "Cannot auto-complete free application; user has a non-terminal subscription in this organization."
        )

    existing = OrganizationMember.objects.filter(
        organization=application.organization,
        user=application.user,
        tier=application.tier,
    ).first()
    if existing is not None and existing.status == OrganizationMember.MembershipStatus.PAUSED:
        # PAUSED is admin/Stripe-imposed; user must not self-clear it via free apply.
        raise ValueError("Cannot auto-complete free application; user's membership at this tier is paused.")

    OrganizationMember.objects.update_or_create(
        organization=application.organization,
        user=application.user,
        defaults={
            "tier": application.tier,
            "status": OrganizationMember.MembershipStatus.ACTIVE,
        },
    )
    application.status = OrganizationMembershipRequest.Status.COMPLETED
    application.save(update_fields=["status", "updated_at"])

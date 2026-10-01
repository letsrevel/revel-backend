"""Gentle, capped nudges to owners of stalled organizations.

Each rule detects one stalled state. Once a day (``events.send_org_nudges``, shipped
disabled) every eligible org gets at most ONE nudge — the highest-priority rule that
matches and still has sends left. The caps live in ``OrganizationNudge`` (never pruned),
so "at most N, then silence" survives notification retention.

``manage.py org_nudges`` runs the same planner as a dry run by default.
"""

import datetime
import typing as t
from dataclasses import dataclass

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from accounts.utils.email_normalization import normalize_email_for_matching
from common.models import SiteSettings
from events.models import Event, Organization, OrganizationNudge
from notifications.enums import NotificationType
from notifications.service.dispatcher import create_notification
from notifications.service.email_policy import may_email, suppressed_addresses

logger = structlog.get_logger(__name__)

Trigger = OrganizationNudge.Trigger

NEW_ORG_GRACE = datetime.timedelta(days=7)
NUDGE_SPACING = datetime.timedelta(days=14)
DRAFT_STALE_AFTER = datetime.timedelta(days=14)
NO_EVENTS_AFTER = datetime.timedelta(days=14)
DORMANT_AFTER = datetime.timedelta(days=90)

CAPS: dict[str, int] = {
    Trigger.DRAFT_EVENT: 2,
    Trigger.PRIVATE_PROFILE: 2,
    Trigger.NO_EVENTS: 2,
    Trigger.CHECK_IN: 1,
    Trigger.DORMANT: 1,
}

_HIDDEN_VISIBILITIES = {Organization.Visibility.PRIVATE, Organization.Visibility.STAFF_ONLY}
_PUBLISHED_STATUSES = [Event.EventStatus.OPEN, Event.EventStatus.CLOSED]


@dataclass(frozen=True)
class _Match:
    episode_key: str = ""
    target_event: Event | None = None


@dataclass(frozen=True)
class PlannedNudge:
    """One nudge the planner decided to send."""

    organization: Organization
    trigger: str
    sequence: int
    episode_key: str = ""
    target_event: Event | None = None

    @property
    def is_last(self) -> bool:
        """Whether this is the final nudge for its trigger (the copy says so)."""
        return self.sequence >= CAPS[self.trigger]


def _real_events(org: Organization) -> "QuerySet[Event]":
    return Event.objects.exclude_templates().filter(organization=org)


def _draft_event(org: Organization, now: datetime.datetime) -> _Match | None:
    """A future draft nobody has touched for two weeks (most recently edited one)."""
    draft = (
        _real_events(org)
        .filter(status=Event.EventStatus.DRAFT, updated_at__lt=now - DRAFT_STALE_AFTER, start__gt=now)
        .order_by("-updated_at")
        .first()
    )
    return _Match(target_event=draft) if draft else None


def _private_profile(org: Organization, now: datetime.datetime) -> _Match | None:
    """Profile hidden from everyone but staff (PRIVATE is the default). MEMBERS_ONLY/UNLISTED are deliberate."""
    return _Match() if org.visibility in _HIDDEN_VISIBILITIES else None


def _no_events(org: Organization, now: datetime.datetime) -> _Match | None:
    """Two weeks in and not a single event, not even a draft."""
    if org.created_at > now - NO_EVENTS_AFTER or _real_events(org).exists():
        return None
    return _Match()


def _check_in(org: Organization, now: datetime.datetime) -> _Match | None:
    """Never published anything, and the automated nudges already had their go.

    Ranked below every fixable trigger, so it only wins once those are exhausted or moot.
    """
    if not settings.ORG_NUDGE_REPLY_TO:  # "reply and tell me" with nobody on the other end would be a lie
        return None
    if _real_events(org).filter(status__in=_PUBLISHED_STATUSES).exists() or not org.nudges.exists():
        return None
    return _Match()


def _dormant(org: Organization, now: datetime.datetime) -> _Match | None:
    """Published before, quiet for 90 days, nothing upcoming. Re-arms per last event."""
    last = _real_events(org).filter(status__in=_PUBLISHED_STATUSES).order_by("-end").first()
    if last is None or last.end > now - DORMANT_AFTER:
        return None
    if _real_events(org).filter(start__gt=now).exclude(status=Event.EventStatus.CANCELLED).exists():
        return None
    return _Match(episode_key=str(last.id), target_event=last)


# Priority order: concrete fixes first, then the human check-in, then re-engagement.
RULES: list[tuple[str, t.Callable[[Organization, datetime.datetime], _Match | None]]] = [
    (Trigger.DRAFT_EVENT, _draft_event),
    (Trigger.PRIVATE_PROFILE, _private_profile),
    (Trigger.NO_EVENTS, _no_events),
    (Trigger.CHECK_IN, _check_in),
    (Trigger.DORMANT, _dormant),
]


def _plan_for_org(org: Organization, now: datetime.datetime) -> PlannedNudge | None:
    """Highest-priority matching rule with sends left, honouring the spacing between nudges."""
    if org.nudges.filter(created_at__gt=now - NUDGE_SPACING).exists():
        return None
    for trigger, rule in RULES:
        match = rule(org, now)
        if match is None:
            continue
        sent = org.nudges.filter(trigger=trigger, episode_key=match.episode_key).count()
        if sent >= CAPS[trigger]:
            continue
        return PlannedNudge(org, trigger, sent + 1, match.episode_key, match.target_event)
    return None


def _eligible_organizations(now: datetime.datetime, organization: Organization | None) -> list[Organization]:
    """Orgs past the grace period whose owner is a real, reachable, opted-in user."""
    qs = Organization.objects.filter(
        created_at__lte=now - NEW_ORG_GRACE,
        owner__is_active=True,
        owner__guest=False,
        owner__email_verified=True,
    ).select_related("owner__notification_preferences")
    if organization is not None:
        qs = qs.filter(pk=organization.pk)
    # ponytail: per-org rule queries (~5 each) — fine for hundreds of orgs on a daily beat;
    # batch the rule queries with annotations if this ever runs over thousands.
    orgs = list(qs)
    suppressed = suppressed_addresses(org.owner.email for org in orgs)
    return [
        org
        for org in orgs
        # Check opt-out/suppression up front so an unreachable owner never burns a cap.
        if may_email(org.owner, NotificationType.ORG_SETUP_NUDGE)
        and normalize_email_for_matching(org.owner.email) not in suppressed
    ]


def plan_nudges(now: datetime.datetime | None = None, organization: Organization | None = None) -> list[PlannedNudge]:
    """Who would be nudged right now, and why. Read-only.

    Args:
        now: Reference time (defaults to now).
        organization: Restrict planning to this org.

    Returns:
        At most one planned nudge per eligible organization.
    """
    now = now or timezone.now()
    plans = (_plan_for_org(org, now) for org in _eligible_organizations(now, organization))
    return [plan for plan in plans if plan is not None]


def _context(plan: PlannedNudge) -> dict[str, t.Any]:
    org = plan.organization
    org_admin_url = f"{SiteSettings.get_solo().frontend_base_url}/org/{org.slug}/admin"
    action_urls: dict[str, str] = {
        Trigger.DRAFT_EVENT: f"{org_admin_url}/events/{plan.target_event.id if plan.target_event else ''}/edit",
        Trigger.PRIVATE_PROFILE: f"{org_admin_url}/settings",
        Trigger.NO_EVENTS: f"{org_admin_url}/events/new",
        Trigger.CHECK_IN: "",
        Trigger.DORMANT: f"{org_admin_url}/events/new",
    }
    context: dict[str, t.Any] = {
        "trigger": plan.trigger,
        "is_last": plan.is_last,
        "organization_name": org.name,
        "organization_slug": org.slug,
        "action_url": action_urls[plan.trigger],
        "frontend_url": action_urls[plan.trigger] or org_admin_url,
        "can_reply": bool(settings.ORG_NUDGE_REPLY_TO),
    }
    if plan.target_event is not None:
        context["event_name"] = plan.target_event.name
    if plan.trigger == Trigger.CHECK_IN:
        context["signature"] = settings.ORG_NUDGE_SIGNATURE
    return context


def send_nudge(organization: Organization, now: datetime.datetime | None = None) -> PlannedNudge | None:
    """Re-plan one org under a row lock and send its nudge, if it still has one.

    The lock serialises overlapping runs (spacing and caps are re-checked inside it);
    the unique constraint on OrganizationNudge is the backstop.

    Args:
        organization: The organization to nudge.
        now: Reference time (defaults to now).

    Returns:
        The nudge that was sent, or None if the org no longer qualifies.
    """
    from notifications.tasks import dispatch_notification

    now = now or timezone.now()
    with transaction.atomic():
        org = Organization.objects.select_for_update().select_related("owner").get(pk=organization.pk)
        plan = _plan_for_org(org, now)
        if plan is None:
            return None
        notification = create_notification(NotificationType.ORG_SETUP_NUDGE, org.owner, _context(plan))
        OrganizationNudge.objects.create(
            organization=org,
            trigger=plan.trigger,
            episode_key=plan.episode_key,
            sequence=plan.sequence,
            target_event=plan.target_event,
            notification=notification,
        )
        transaction.on_commit(lambda: dispatch_notification.delay(str(notification.id)))
    logger.info(
        "org_nudge_sent",
        organization_id=str(org.id),
        trigger=plan.trigger,
        sequence=plan.sequence,
    )
    return plan


def send_nudges(now: datetime.datetime | None = None, organization: Organization | None = None) -> list[PlannedNudge]:
    """Plan and send today's nudges.

    Args:
        now: Reference time (defaults to now).
        organization: Restrict to this org.

    Returns:
        The nudges actually sent.
    """
    now = now or timezone.now()
    sent = [send_nudge(plan.organization, now) for plan in plan_nudges(now, organization)]
    return [plan for plan in sent if plan is not None]

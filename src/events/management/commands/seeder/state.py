"""State tracking for the seeder."""

import random
import typing as t
from dataclasses import dataclass, field
from uuid import UUID

if t.TYPE_CHECKING:
    from accounts.models import FoodItem, RevelUser
    from common.models import Tag
    from events.models import (
        Event,
        EventSeries,
        MembershipTier,
        Organization,
        OrganizationMember,
        TicketTier,
        Venue,
        VenueSector,
    )
    from questionnaires.models import Questionnaire


@dataclass
class SeederState:
    """Tracks created objects for cross-module references."""

    # Pre-computed values for performance
    hashed_password: str = ""
    placeholder_png: bytes = b""
    placeholder_pdf: bytes = b""

    # Core entities - lists
    users: list["RevelUser"] = field(default_factory=list)
    organizations: list["Organization"] = field(default_factory=list)
    events: list["Event"] = field(default_factory=list)
    event_series: list["EventSeries"] = field(default_factory=list)
    questionnaires: list["Questionnaire"] = field(default_factory=list)
    tags: list["Tag"] = field(default_factory=list)
    food_items: list["FoodItem"] = field(default_factory=list)

    # Categorized users for role assignment
    owner_users: list["RevelUser"] = field(default_factory=list)
    staff_users: list["RevelUser"] = field(default_factory=list)
    member_users: list["RevelUser"] = field(default_factory=list)
    regular_users: list["RevelUser"] = field(default_factory=list)

    # Lookups by organization ID
    membership_tiers: dict[UUID, list["MembershipTier"]] = field(default_factory=dict)
    org_members: dict[UUID, list["OrganizationMember"]] = field(default_factory=dict)
    venues: dict[UUID, list["Venue"]] = field(default_factory=dict)
    org_events: dict[UUID, list["Event"]] = field(default_factory=dict)
    org_questionnaires: dict[UUID, list["Questionnaire"]] = field(default_factory=dict)

    # Lookups by venue ID
    venue_sectors: dict[UUID, list["VenueSector"]] = field(default_factory=dict)

    # Lookups by event ID
    ticket_tiers: dict[UUID, list["TicketTier"]] = field(default_factory=dict)

    # Edge case tracking
    sold_out_events: list["Event"] = field(default_factory=list)
    waitlist_events: list["Event"] = field(default_factory=list)
    potluck_events: list["Event"] = field(default_factory=list)
    past_events: list["Event"] = field(default_factory=list)
    ticketed_events: list["Event"] = field(default_factory=list)
    non_ticketed_events: list["Event"] = field(default_factory=list)
    private_events: list["Event"] = field(default_factory=list)

    def get_random_user(self, rand: random.Random) -> "RevelUser":
        """Get a random user from all users."""
        return rand.choice(self.users)

    def get_random_member_for_org(self, org_id: UUID, rand: random.Random) -> "OrganizationMember | None":
        """Get a random member for an organization."""
        members = self.org_members.get(org_id, [])
        return rand.choice(members) if members else None

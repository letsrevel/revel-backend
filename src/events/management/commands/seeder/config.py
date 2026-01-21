"""Configuration for the seeder."""

from dataclasses import dataclass, field


@dataclass
class SeederConfig:
    """Configuration for seed data generation."""

    seed: int
    num_users: int = 1000
    num_organizations: int = 20
    num_events_per_org: int = 50
    clear_data: bool = False

    # Ratios for derived counts
    staff_per_org: tuple[int, int] = (2, 10)
    members_per_org: tuple[int, int] = (10, 100)
    venues_per_org: tuple[int, int] = (1, 3)
    sectors_per_venue: tuple[int, int] = (2, 5)
    tiers_per_event: tuple[int, int] = (1, 4)
    questionnaires_per_org: tuple[int, int] = (1, 3)

    # Distribution weights for visibility
    visibility_weights: dict[str, float] = field(
        default_factory=lambda: {
            "public": 0.4,
            "private": 0.3,
            "members_only": 0.2,
            "staff_only": 0.1,
        }
    )

    # Distribution weights for event status
    event_status_weights: dict[str, float] = field(
        default_factory=lambda: {
            "open": 0.5,
            "closed": 0.2,
            "draft": 0.2,
            "cancelled": 0.1,
        }
    )

    # Distribution weights for ticket payment methods
    payment_method_weights: dict[str, float] = field(
        default_factory=lambda: {
            "online": 0.3,
            "offline": 0.3,
            "at_the_door": 0.2,
            "free": 0.2,
        }
    )

    # Distribution weights for membership status
    membership_status_weights: dict[str, float] = field(
        default_factory=lambda: {
            "active": 0.80,
            "paused": 0.10,
            "cancelled": 0.05,
            "banned": 0.05,
        }
    )

    # Distribution weights for ticket status
    ticket_status_weights: dict[str, float] = field(
        default_factory=lambda: {
            "active": 0.70,
            "pending": 0.10,
            "checked_in": 0.15,
            "cancelled": 0.05,
        }
    )

    # Distribution weights for payment status
    payment_status_weights: dict[str, float] = field(
        default_factory=lambda: {
            "succeeded": 0.80,
            "pending": 0.10,
            "failed": 0.05,
            "refunded": 0.05,
        }
    )

    # Distribution weights for questionnaire evaluation status
    evaluation_status_weights: dict[str, float] = field(
        default_factory=lambda: {
            "approved": 0.60,
            "rejected": 0.25,
            "pending_review": 0.15,
        }
    )

    # Edge case percentages
    sold_out_event_pct: float = 0.10
    waitlist_event_pct: float = 0.15
    potluck_event_pct: float = 0.20
    past_event_pct: float = 0.20
    pwyc_tier_pct: float = 0.20

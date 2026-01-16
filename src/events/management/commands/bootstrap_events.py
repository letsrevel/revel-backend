# src/events/management/commands/bootstrap_events.py
"""Bootstrap comprehensive example data for events, organizations, and questionnaires.

This command creates a realistic dataset for frontend development and testing,
including multiple organizations, diverse events, users with various relationships,
tags, potluck items, questionnaires, and more.
"""

import typing as t

import structlog
from django.core.management.base import BaseCommand

from .bootstrap_helpers import (
    BootstrapState,
    create_dietary_data,
    create_event_series,
    create_events,
    create_follows,
    create_organizations,
    create_potluck_items,
    create_questionnaires,
    create_tags,
    create_ticket_tiers,
    create_user_relationships,
    create_users,
    create_venues,
)

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    """Bootstrap comprehensive example data for events, organizations, and questionnaires.

    This command creates a realistic dataset for frontend development and testing,
    including multiple organizations, diverse events, users with various relationships,
    tags, potluck items, questionnaires, and more.
    """

    help = "Bootstrap comprehensive example data for events, organizations, and questionnaires."

    def handle(self, *args: t.Any, **options: t.Any) -> None:  # pragma: no cover
        """Bootstrap example data."""
        logger.info("Starting bootstrap process...")

        state = BootstrapState()

        # Load cities
        state.load_cities()

        # Create tags
        create_tags(state)

        # Create users
        create_users(state)

        # Create organizations
        create_organizations(state)

        # Create venues
        create_venues(state)

        # Create event series
        create_event_series(state)

        # Create events
        create_events(state)

        # Create ticket tiers
        create_ticket_tiers(state)

        # Create potluck items
        create_potluck_items(state)

        # Create user relationships
        create_user_relationships(state)

        # Create follow relationships
        create_follows(state)

        # Create dietary data
        create_dietary_data(state)

        # Create questionnaires
        create_questionnaires(state)

        logger.info("Bootstrap complete! See README.md in this directory for details.")

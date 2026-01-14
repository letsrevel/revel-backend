# src/events/management/commands/bootstrap_helpers/tags.py
"""Tag creation for bootstrap process."""

import structlog

from common.models import Tag

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def create_tags(state: BootstrapState) -> None:
    """Create a comprehensive tag taxonomy."""
    logger.info("Creating tags...")

    # Category tags
    category_tags = {
        "music": {"description": "Music-related events", "color": "#FF6B6B"},
        "food": {"description": "Food and dining events", "color": "#4ECDC4"},
        "workshop": {"description": "Educational workshops", "color": "#45B7D1"},
        "conference": {"description": "Professional conferences", "color": "#96CEB4"},
        "networking": {"description": "Networking events", "color": "#FFEAA7"},
        "arts": {"description": "Arts and culture", "color": "#DDA15E"},
        "tech": {"description": "Technology events", "color": "#6C5CE7"},
        "sports": {"description": "Sports and fitness", "color": "#00B894"},
        "wellness": {"description": "Health and wellness", "color": "#FDCB6E"},
        "community": {"description": "Community gatherings", "color": "#E17055"},
    }

    # Vibe tags
    vibe_tags = {
        "casual": {"description": "Relaxed atmosphere", "color": "#74B9FF"},
        "formal": {"description": "Formal dress code", "color": "#2D3436"},
        "educational": {"description": "Learning focused", "color": "#00CEC9"},
        "social": {"description": "Social interaction", "color": "#FD79A8"},
        "professional": {"description": "Professional setting", "color": "#636E72"},
    }

    all_tags = {**category_tags, **vibe_tags}

    for tag_name, tag_data in all_tags.items():
        tag, created = Tag.objects.get_or_create(
            name=tag_name,
            defaults={
                "description": tag_data["description"],
                "color": tag_data["color"],
            },
        )
        state.tags[tag_name] = tag

    logger.info(f"Created {len(state.tags)} tags")

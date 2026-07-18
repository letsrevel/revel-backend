"""Regression: editor-built venues must yield adjacent pairs for best-available.

Before rank derivation landed, seats written through the admin API kept
``row_order``/``adjacency_index`` at their default 0, so contiguous-run
detection degenerated into single-seat runs and ``pick_best_available`` could
never find an adjacent pair on a user-built venue (every 2+ hold 409'd).
"""

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Organization, PriceCategory, TicketTier, Venue, VenueSeat, VenueSector
from events.service.seating.best_available import pick_best_available
from events.service.seating.pick import load_candidates

pytestmark = pytest.mark.django_db


def test_editor_built_sector_gets_adjacent_pair_from_best_available(
    organization_owner_client: Client, organization: Organization, event: Event
) -> None:
    """Grid-editor-shaped bulk create (no explicit ranks) must produce pickable adjacent pairs."""
    venue = Venue.objects.create(organization=organization, name="Editor Hall")
    sector = VenueSector.objects.create(venue=venue, name="Stalls")
    category = PriceCategory.objects.create(venue=venue, name="Standard", color="#0000aa")

    url = reverse(
        "api:bulk_create_venue_seats",
        kwargs={"slug": organization.slug, "venue_id": venue.id, "sector_id": sector.id},
    )
    payload = {
        "seats": [
            {"label": f"{row}{num}", "row": row, "number": num, "price_category_id": str(category.id)}
            for row in ("A", "B")
            for num in (1, 2, 3, 4)
        ]
    }
    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
    assert response.status_code == 201, response.content

    event.venue = venue
    event.save(update_fields=["venue"])
    tier = TicketTier.objects.create(
        event=event,
        name="Standard",
        price_category=category,
        seat_assignment_mode=TicketTier.SeatAssignmentMode.BEST_AVAILABLE,
    )

    candidates = load_candidates(event, tier, set())
    picked = pick_best_available(candidates, 2, seed=1)

    assert len(picked) == 2, "best-available found no adjacent pair on an editor-built venue"
    seats = list(VenueSeat.objects.filter(id__in=picked))
    assert seats[0].row_label == seats[1].row_label
    assert abs(seats[0].adjacency_index - seats[1].adjacency_index) == 1

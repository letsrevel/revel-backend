import os
import django


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "revel.settings")

django.setup()

# imports start here
from pathlib import Path
from django.template.loader import render_to_string
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from accounts.models import RevelUser
from events.models import Event, Organization, Ticket, TicketTier, Payment, EventRSVP, PotluckItem
from geo.models import City
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.utils import get_formatted_context_for_template


def create_test_data():
    """Create test data for all notification types."""
    print("Creating test data...")

    # Create user
    user, _ = RevelUser.objects.get_or_create(
        email="test@example.com",
        defaults={
            "username": "testuser",
            "first_name": "John",
            "last_name": "Doe",
        }
    )

    # Create city
    city, _ = City.objects.get_or_create(
        name="New York",
        defaults={"country": "US", "state": "NY"}
    )

    # Create organization
    org, _ = Organization.objects.get_or_create(
        name="Test Organization",
        defaults={
            "slug": "test-org",
            "city": city,
            "owner": user,
        }
    )

    # Create event
    event, _ = Event.objects.get_or_create(
        name="Test Production Event",
        defaults={
            "slug": "test-event",
            "organization": org,
            "start": timezone.now() + timedelta(days=7),
            "end": timezone.now() + timedelta(days=7, hours=3),
            "address": "123 Main Street",
            "city": city,
            "description": "This is a test event with full details.",
            "status": Event.EventStatus.OPEN,
        }
    )

    # Create ticket tier
    tier, _ = TicketTier.objects.get_or_create(
        event=event,
        name="General Admission",
        defaults={
            "price": Decimal("25.00"),
            "payment_method": TicketTier.PaymentMethod.ONLINE,
            "max_tickets": 100,
        }
    )

    # Create ticket
    ticket, _ = Ticket.objects.get_or_create(
        event=event,
        user=user,
        tier=tier,
        defaults={
            "status": Ticket.TicketStatus.ACTIVE,
        }
    )

    # Create payment
    payment, _ = Payment.objects.get_or_create(
        ticket=ticket,
        user=user,
        stripe_session_id="test_session_123",
        defaults={
            "amount": Decimal("25.00"),
            "platform_fee": Decimal("2.50"),
            "currency": "USD",
            "status": Payment.PaymentStatus.SUCCEEDED,
        }
    )

    # Create RSVP
    rsvp, _ = EventRSVP.objects.get_or_create(
        event=event,
        user=user,
        defaults={
            "status": EventRSVP.RsvpStatus.YES,
        }
    )

    # Create potluck item
    potluck, _ = PotluckItem.objects.get_or_create(
        event=event,
        name="Potato Salad",
        defaults={
            "item_type": PotluckItem.ItemTypes.SIDE_DISH,
            "quantity": 2,
            "note": "Please bring enough for 10 people",
            "created_by": user,
        }
    )

    print("✓ Test data created")
    return user, event, ticket, payment, rsvp, potluck, org


def build_notification_contexts(user, event, ticket, payment, rsvp, potluck, org):
    """Build all notification contexts."""
    from django.utils.dateformat import format as date_format
    from common.models import SiteSettings

    frontend_url = SiteSettings.get_solo().frontend_base_url

    contexts = {}

    # Event notifications
    contexts["event_cancelled"] = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "refund_info": "Refunds will be processed according to the organizer's refund policy.",
        "cancellation_reason": "Due to unforeseen circumstances, we must cancel this event.",
    }

    contexts["event_updated"] = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_end_formatted": date_format(event.end, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "changes_summary": "start, location",
        "update_message": "Start: January 1, 2025 at 6:00 PM → January 5, 2025 at 7:00 PM; Location: TBD → 123 Main Street, New York",
    }

    contexts["event_open"] = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_description": event.description,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_end_formatted": date_format(event.end, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "organization_name": org.name,
    }

    # Payment notification
    contexts["payment_confirmation"] = {
        "ticket_id": str(ticket.id),
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "tier_name": ticket.tier.name,
        "payment_amount": str(payment.amount),
        "payment_currency": payment.currency,
        "payment_id": str(payment.id),
        "payment_date": date_format(payment.created_at, "l, F j, Y \\a\\t g:i A T"),
    }

    # Ticket notifications
    contexts["ticket_created"] = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "ticket_id": str(ticket.id),
        "tier_name": ticket.tier.name,
        "ticket_status": ticket.status,
    }

    contexts["ticket_updated"] = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "ticket_id": str(ticket.id),
        "tier_name": ticket.tier.name,
        "ticket_status": "active",
        "old_status": "pending",
        "new_status": "active",
        "action": "activated",
    }

    contexts["ticket_cancelled"] = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "ticket_id": str(ticket.id),
        "tier_name": ticket.tier.name,
        "cancellation_reason": "Unable to attend",
    }

    # RSVP notifications
    contexts["rsvp_confirmation"] = {
        "rsvp_id": str(rsvp.id),
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "response": "yes",
        "user_name": user.get_display_name(),
        "user_email": user.email,
        "guest_count": 2,
        "dietary_restrictions": "Vegetarian",
    }

    contexts["rsvp_updated"] = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "old_response": "yes",
        "new_response": "no",
        "user_name": user.get_display_name(),
        "guest_count": 0,
    }

    contexts["rsvp_cancelled"] = {
        "event_id": str(event.id),
        "event_name": event.name,
        "event_start_formatted": date_format(event.start, "l, F j, Y \\a\\t g:i A T"),
        "event_location": event.full_address(),
        "event_url": f"{frontend_url}/events/{event.id}",
        "user_name": user.get_display_name(),
        "cancellation_reason": "Schedule conflict",
    }

    # Potluck notifications
    contexts["potluck_item_created"] = {
        "potluck_item_id": str(potluck.id),
        "item_name": potluck.name,
        "event_id": str(event.id),
        "event_name": event.name,
        "action": "created",
        "event_url": f"{frontend_url}/events/{event.id}",
        "item_type": "Salad",
        "quantity": 2,
        "note": potluck.note,
        "actor_name": user.get_display_name(),
        "is_organizer": False,
    }

    return contexts


def render_all_templates(user, contexts):
    """Render all notification templates."""
    output_dir = Path("/Users/biagio/repos/letsrevel/revel-backend/rendered_templates")
    output_dir.mkdir(exist_ok=True)

    print(f"\nRendering templates to {output_dir}/")

    notification_types = [
        "event_cancelled",
        "event_updated",
        "event_open",
        "payment_confirmation",
        "ticket_created",
        "ticket_updated",
        "ticket_cancelled",
        "rsvp_confirmation",
        "rsvp_updated",
        "rsvp_cancelled",
        "potluck_item_created",
    ]

    channels = ["email", "in_app", "telegram"]

    for notif_type in notification_types:
        if notif_type not in contexts:
            continue

        print(f"\n{notif_type}:")
        context = contexts[notif_type]

        # Add enriched context
        from notifications.service.unsubscribe import generate_unsubscribe_token
        from common.models import SiteSettings

        enriched_context = get_formatted_context_for_template(context, user_language="en")
        unsubscribe_token = generate_unsubscribe_token(user)
        site_settings = SiteSettings.get_solo()
        enriched_context["unsubscribe_link"] = f"{site_settings.frontend_base_url}/unsubscribe?token={unsubscribe_token}"

        template_context = {
            "user": user,
            "context": enriched_context,
        }

        for channel in channels:
            # Render TXT
            txt_template = f"notifications/{channel}/{notif_type}.txt"
            try:
                txt_content = render_to_string(txt_template, template_context)
                txt_file = output_dir / f"{notif_type}_{channel}.txt"
                txt_file.write_text(txt_content)
                print(f"  ✓ {channel}.txt")
            except Exception as e:
                if "email" in channel:  # Email has both txt and html
                    pass
                else:
                    print(f"  ✗ {channel}.txt - {e}")

            # Render HTML (email only)
            if channel == "email":
                html_template = f"notifications/{channel}/{notif_type}.html"
                try:
                    html_content = render_to_string(html_template, template_context)
                    html_file = output_dir / f"{notif_type}_{channel}.html"
                    html_file.write_text(html_content)
                    print(f"  ✓ {channel}.html")
                except Exception as e:
                    print(f"  ✗ {channel}.html - {e}")

            # Render MD (in_app and telegram)
            if channel in ["in_app", "telegram"]:
                md_template = f"notifications/{channel}/{notif_type}.md"
                try:
                    md_content = render_to_string(md_template, template_context)
                    md_file = output_dir / f"{notif_type}_{channel}.md"
                    md_file.write_text(md_content)
                    print(f"  ✓ {channel}.md")
                except Exception as e:
                    print(f"  ✗ {channel}.md - {e}")

    print(f"\n\n✓ All templates rendered to {output_dir}/")
    print(f"\nTo view HTML files, open them in a browser:")
    print(f"  open {output_dir}/*.html")


if __name__ == "__main__":
    print("=" * 80)
    print("NOTIFICATION TEMPLATE RENDERER")
    print("=" * 80)

    user, event, ticket, payment, rsvp, potluck, org = create_test_data()
    contexts = build_notification_contexts(user, event, ticket, payment, rsvp, potluck, org)
    render_all_templates(user, contexts)

    print("\n" + "=" * 80)
    print("DONE!")
    print("=" * 80)

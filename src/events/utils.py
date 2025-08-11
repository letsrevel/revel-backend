import base64
import typing as t
from io import BytesIO

import qrcode
from django.template import Context, Template
from django.template.loader import render_to_string
from weasyprint import HTML

from accounts.models import RevelUser
from events import models

from .models import Ticket


def get_invitation_message(user: RevelUser, event: models.Event) -> str:
    """Get invitation message.

    If the event has a custom invitation message, render it as a Django template.
    Otherwise, use the default template.
    """
    context = {"user": user, "event": event}

    if event.invitation_message:
        template = Template(event.invitation_message)
        return template.render(Context(context))

    return render_to_string("events/default_invitation_message.txt", context=context)


def create_ticket_pdf(ticket: Ticket) -> bytes:
    """Generates a PDF version of a ticket using weasyprint.

    Args:
        ticket: The Ticket object, expected to have related event, user, tier, etc., prefetched.

    Returns:
        The PDF content as bytes.
    """
    # 1. Generate QR Code from the ticket's UUID
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(str(ticket.id))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    # 2. Convert QR code to a base64 string to embed in HTML
    buffered = BytesIO()
    img.save(buffered, "PNG")
    qr_code_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    # 3. Prepare context for the HTML template
    context_data = {
        "event_name": ticket.event.name,
        "organization_name": ticket.event.organization.name,
        "user_display_name": ticket.user.get_display_name(),
        "tier_name": ticket.tier.name,
        "start_datetime": ticket.event.start.strftime("%A, %B %d, %Y at %I:%M %p %Z"),
        "address": ticket.event.address or (ticket.event.city.name if ticket.event.city else ""),
        "qr_code_base64": qr_code_base64,
        "ticket_id": str(ticket.id),
    }

    # 4. Render HTML template from the string
    html_string = render_to_string("events/ticket.html", context=context_data)

    # 5. Generate PDF from HTML using weasyprint
    html = HTML(string=html_string)
    return t.cast(bytes, html.write_pdf())

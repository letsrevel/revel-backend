import os
import django


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "revel.settings")

django.setup()

from accounts.models import RevelUser, UserDataExport
from events.models import Event, Organization, Ticket, Payment, TicketTier
from accounts.tasks import generate_user_data_export
from events.service import stripe_service
from django.utils import timezone
from events.utils import create_ticket_pdf

# owner = RevelUser.objects.create_user(username="test-org-owner@example.com", email="test-org-owner@example.com", password="password")

event = Event.objects.first()
tier = TicketTier.objects.first()
user = RevelUser.objects.first()

ticket = Ticket.objects.create(event=event, user=user, tier=tier)
ticket.save()

data = create_ticket_pdf(ticket)


with open('ticket.pdf', 'wb') as f:
    f.write(data)

with open("event.ics", "wb") as f:
    f.write(event.ics())
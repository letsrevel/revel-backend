"""Preview (default) or send the org setup nudges.

python manage.py org_nudges                 # dry run: who would be nudged, and why
python manage.py org_nudges --org my-org    # dry run for one org
python manage.py org_nudges --send          # actually send (same path as the beat task)
"""

import typing as t

from django.core.management.base import BaseCommand, CommandError, CommandParser

from events.models import Organization
from events.service import org_nudge_service


class Command(BaseCommand):
    help = "Preview (default) or send setup nudges to owners of stalled organizations."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register --send and --org."""
        parser.add_argument("--send", action="store_true", help="Send the nudges instead of previewing them.")
        parser.add_argument("--org", help="Restrict to the organization with this slug.")

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Print the planned (or sent) nudges as a table."""
        organization = None
        if options["org"]:
            organization = Organization.objects.filter(slug=options["org"]).first()
            if organization is None:
                raise CommandError(f"No organization with slug {options['org']!r}.")
        if options["send"]:
            nudges = org_nudge_service.send_nudges(organization=organization)
            verb = "Sent"
        else:
            nudges = org_nudge_service.plan_nudges(organization=organization)
            verb = "Would send (dry run, nothing written)"
        for nudge in nudges:
            org = nudge.organization
            target = f" — {nudge.target_event.name}" if nudge.target_event else ""
            last = " (last)" if nudge.is_last else ""
            self.stdout.write(f"{org.slug:<30} {org.owner.email:<40} {nudge.trigger} #{nudge.sequence}{last}{target}")
        self.stdout.write(self.style.SUCCESS(f"{verb}: {len(nudges)} nudge(s)."))

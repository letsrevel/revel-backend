# Generated manually for data migration
from django.db import migrations, models
from django.db.models import Case, OuterRef, Subquery, Value, When
from django.db.models.functions import Coalesce, Concat, Trim


def populate_guest_names(apps, schema_editor):
    """Populate guest_name from user's preferred_name/full_name/username.

    Mirrors RevelUser.get_display_name() logic:
    - preferred_name if set
    - first_name + ' ' + last_name if either is set
    - username as fallback
    """
    Ticket = apps.get_model("events", "Ticket")
    RevelUser = apps.get_model("accounts", "RevelUser")
    db_alias = schema_editor.connection.alias

    # Build the display name expression using the same logic as get_display_name()
    # preferred_name OR (first_name + ' ' + last_name trimmed) OR username
    display_name_expr = Case(
        # If preferred_name is set and not empty, use it
        When(
            preferred_name__isnull=False,
            preferred_name__gt="",
            then="preferred_name",
        ),
        # Else, try first_name + ' ' + last_name (trimmed in case only one is set)
        default=Case(
            When(
                models.Q(first_name__gt="") | models.Q(last_name__gt=""),
                then=Trim(
                    Concat(
                        Coalesce("first_name", Value("")),
                        Value(" "),
                        Coalesce("last_name", Value("")),
                    )
                ),
            ),
            # Fallback to username
            default="username",
        ),
    )

    # Update tickets where guest_name is null
    Ticket.objects.using(db_alias).filter(guest_name__isnull=True).update(
        guest_name=Subquery(
            RevelUser.objects.filter(pk=OuterRef("user_id"))
            .annotate(computed_display_name=display_name_expr)
            .values("computed_display_name")[:1]
        )
    )

    # Safety net: if any tickets still have null/empty guest_name, use "Guest"
    Ticket.objects.using(db_alias).filter(
        models.Q(guest_name__isnull=True) | models.Q(guest_name="")
    ).update(guest_name="Guest")


def reverse_populate_guest_names(apps, schema_editor):
    """Reverse operation - set all guest_names to null."""
    Ticket = apps.get_model("events", "Ticket")
    db_alias = schema_editor.connection.alias
    Ticket.objects.using(db_alias).all().update(guest_name=None)


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0024_multi_ticket_support"),
    ]

    operations = [
        migrations.RunPython(
            populate_guest_names,
            reverse_code=reverse_populate_guest_names,
        ),
    ]

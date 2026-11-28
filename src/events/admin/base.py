# src/events/admin/base.py
"""Base admin components: mixins, forms, and inlines."""

import functools
import typing as t

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from unfold.admin import TabularInline
from unfold.contrib.filters.admin import RangeDateFilter
from unfold.utils import parse_date_str
from unfold.widgets import CHECKBOX_CLASSES

from common.models import SiteSettings
from events import models


def frontend_url(path: str) -> str:
    """Absolute Revel frontend URL for ``path`` (e.g. ``/org/my-slug``)."""
    return f"{SiteSettings.get_solo().frontend_base_url.rstrip('/')}{path}"


def format_city(city: t.Any) -> str:
    """Format a ``geo.City`` as ``"Name (Country)"``; em dash when there is none."""
    return f"{city.name} ({city.country})" if city else "—"


def view_on_revel_link(url: str | None) -> str:
    """Render a new-tab link to the Revel frontend, or an em dash when there is no URL."""
    if not url:
        return "—"
    return format_html('<a href="{}" target="_blank" rel="noopener">View ↗</a>', url)


class InclusiveRangeDateFilter(RangeDateFilter):  # type: ignore[misc]
    """``RangeDateFilter`` for DateTimeFields that includes the whole "to" day.

    Unfold compares ``field__lte=<date>``, which a DateTimeField reads as midnight, dropping
    everything created later that day. Comparing on ``__date`` (current timezone) fixes both ends.
    """

    def queryset(self, request: HttpRequest, queryset: QuerySet[t.Any]) -> QuerySet[t.Any] | None:
        filters: dict[str, t.Any] = {}
        value_from = self.used_parameters.get(f"{self.parameter_name}_from")
        if value_from and isinstance(value_from, str):
            filters[f"{self.parameter_name}__date__gte"] = parse_date_str(value_from)
        value_to = self.used_parameters.get(f"{self.parameter_name}_to")
        if value_to and isinstance(value_to, str):
            filters[f"{self.parameter_name}__date__lte"] = parse_date_str(value_to)
        try:
            return queryset.filter(**filters)
        except ValueError, ValidationError:
            return None


# --- Helper Mixins for Reusable Link Fields ---
class CreateOnlyFieldsMixin:
    """Disable ``create_only_fields`` on the change page; they stay editable on the add page (#1596).

    A disabled field ignores a posted value and keeps the stored one. It stays on the form, unlike
    ``readonly_fields``, so the form still checks the unique constraints that include it: a duplicate name is a
    form error, not a 500 from ``save()``.
    """

    create_only_fields: t.ClassVar[tuple[str, ...]] = ()

    def get_form(self, request: HttpRequest, obj: t.Any = None, change: bool = False, **kwargs: t.Any) -> t.Any:
        form = super().get_form(request, obj, change=change, **kwargs)  # type: ignore[misc]
        if obj is not None:
            for name in self.create_only_fields:
                if name in form.base_fields:  # a user without change permission gets a form with no fields
                    form.base_fields[name].disabled = True
        return form


def edited_fields(form: forms.BaseForm, skip: str | None = None) -> list[str]:
    """Names of the model columns that a saved ``form`` may write.

    These are the enabled form fields that are concrete, non-primary-key fields of the model, minus ``skip`` (the
    inline foreign key), plus every ``auto_now`` field (``updated_at``). All enabled fields are used, not only
    ``form.changed_data``: a model ``clean()`` can fill a field from another one (for example the venue and sector
    of a ticket from its seat), and such a field is on the form too. Readonly and disabled fields are never listed.
    """
    instance = form.instance  # type: ignore[attr-defined]
    concrete = {f.name for f in instance._meta.concrete_fields if not f.primary_key}
    names = [name for name, field in form.fields.items() if not field.disabled and name in concrete and name != skip]
    # Django stamps an ``auto_now`` field only when it is in ``update_fields``; a sweep finds changed rows by it.
    names += [f.name for f in instance._meta.concrete_fields if getattr(f, "auto_now", False) and f.name not in names]
    return names


def _normalized_text(value: t.Any) -> str:
    """Text as a browser round trip leaves it: one line break style, no space at the ends."""
    return ("" if value is None else str(value)).replace("\r\n", "\n").replace("\r", "\n").strip()


def _is_stale(field: forms.Field, initial: t.Any, posted_initial: t.Any) -> bool:
    """Whether the stored value differs from the hidden value of the page."""
    # ``JSONField`` is a ``CharField`` subclass, but its value is a structure: ``has_changed`` compares it right.
    if isinstance(field, forms.CharField) and not isinstance(field, forms.JSONField):
        return _normalized_text(field.prepare_value(initial)) != _normalized_text(posted_initial)
    return field.has_changed(initial, posted_initial)


class EditGuardForm(forms.ModelForm):  # type: ignore[type-arg]
    """Refuse a save that would revert a value which changed after the page opened (optimistic concurrency).

    Django reloads the object at ``POST``, but the posted values come from the page of the ``GET``. A service write
    in between (for example ``is_active`` set by a service, or a seat moved by the box office) would be put back
    silently. Each enabled field of an existing row renders a hidden ``initial-<name>`` input with the value of the
    ``GET`` (``show_hidden_initial``). On save, a field whose stored value differs from that hidden value is a form
    error, and the person must reload the page and make the change again. A missing hidden input counts as stale
    when the stored value is not empty. A form that is re-rendered after an error keeps the old hidden value, so the
    baseline stays the one of the first ``GET`` until a reload.

    Text fields are compared after the same clean-up that a browser and ``CharField`` apply: every line break style
    becomes LF and the ends are stripped. A browser changes every LF to CRLF on submit, and ``CharField`` strips
    only the posted side, so a plain comparison would refuse every save of a value with a line break or a space at
    an end.

    A digest of ``updated_at`` does not work here: services write with ``QuerySet.update`` and ``update_fields``,
    often without ``updated_at``. An inline row is checked only when the user changed it, so an untouched row that a
    service changed is not saved and the service write stays.

    Limits: Django renders a hidden initial input with a ``to_python`` call, so each non-null foreign key of an
    inline row costs one more query on the ``POST`` of the event page. ``save_m2m`` still replaces the whole set of a
    many-to-many field, and the race between the load and the save inside one request remains (see
    :class:`SaveEditedFieldsMixin` for the columns it writes).
    """

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Ask each guarded field to render its hidden initial value."""
        super().__init__(*args, **kwargs)
        for field in self._guarded_fields().values():
            field.show_hidden_initial = True

    def _guarded_fields(self) -> dict[str, forms.Field]:
        """The enabled, non-file fields of an existing row. ``disabled`` is read here, as it can change after init."""
        if self.instance._state.adding:
            return {}
        return {
            name: field
            for name, field in self.fields.items()
            if not field.disabled and not isinstance(field, forms.FileField)
        }

    def clean(self) -> dict[str, t.Any] | None:
        cleaned_data = super().clean()
        # An inline row (it has a prefix) is checked only when the user changed it.
        if self.prefix is not None and not self.has_changed():
            return cleaned_data
        for name, field in self._guarded_fields().items():
            if not field.show_hidden_initial:
                continue
            bound_field = self[name]
            posted_initial = self._widget_data_value(  # type: ignore[attr-defined]
                field.hidden_widget(), bound_field.html_initial_name
            )
            if _is_stale(field, bound_field.initial, posted_initial):
                self.add_error(
                    name,
                    _("This value changed since you opened the page. Reload the page and make your change again."),
                )
        return cleaned_data


class SaveEditedFieldsMixin:
    """On a change save, write only the columns that the form edits.

    A plain ``obj.save()`` writes every column, also the ones that services change at the same time (counters such
    as ``quantity_sold``, ``status``, ``cancelled_at``). Such a write would put back the values that were read when
    the page opened. With ``update_fields`` the readonly and disabled fields are never written. PostgreSQL locks
    the row for the ``UPDATE``: it waits for a service write that is not yet committed, and then sets only the
    listed columns.

    Do not use it for a model whose ``save()`` writes columns that are not form fields (for example the end and
    location of an ``Event``, or a slug and an image).
    """

    def save_model(self, request: HttpRequest, obj: t.Any, form: t.Any, change: bool) -> None:
        if change:
            obj.save(update_fields=edited_fields(form))
        else:
            super().save_model(request, obj, form, change)  # type: ignore[misc]


class SaveEditedFieldsFormSetMixin:
    """Inline formset that saves an existing row with ``update_fields``; see :class:`SaveEditedFieldsMixin`."""

    def save_existing(self, form: t.Any, obj: t.Any, commit: bool = True) -> t.Any:
        obj = form.save(commit=False)
        if commit:
            obj.save(update_fields=edited_fields(form, skip=self.fk.name))  # type: ignore[attr-defined]
            form.save_m2m()
        return obj


class UserLinkMixin:
    """Mixin to add a link to a user."""

    def user_link(self, obj: t.Any) -> str:
        user = getattr(obj, "user", getattr(obj, "owner", None))
        url = reverse("admin:accounts_reveluser_change", args=[user.id])  # type: ignore[union-attr]
        return format_html('<a href="{}">{}</a>', url, user.username)  # type: ignore[union-attr]

    user_link.short_description = "User"  # type: ignore[attr-defined]


class OrganizationLinkMixin:
    """Mixin to add a link to an organization."""

    def organization_link(self, obj: t.Any) -> str | None:
        if not hasattr(obj, "organization") or not obj.organization:
            return None
        url = reverse("admin:events_organization_change", args=[obj.organization.id])
        return format_html('<a href="{}">{}</a>', url, obj.organization.name)

    organization_link.short_description = "Organization"  # type: ignore[attr-defined]


class EmailDeliveryAdminMixin:
    """Surfaces ``EmailDeliverableMixin`` delivery state and a retry action.

    Shared by the financial-document admins. Subclasses implement :meth:`_redeliver`
    to re-dispatch their model's delivery task. The retry action clears the terminal
    ``email_delivery_failed_at`` state (set when a document had no recipient / its org
    was deleted) and re-dispatches, so an operator can re-attempt once the recipient
    is fixed (issue #618).
    """

    # Fields to expose read-only on the change form (add to a "Delivery" fieldset).
    email_delivery_fields = ["email_sent_at", "email_delivery_failed_at", "email_delivery_error"]

    @admin.display(description="Delivery")
    def delivery_status(self, obj: t.Any) -> str:
        if obj.email_sent_at:
            return format_html('<span style="color: green;">Sent</span>')
        if obj.email_delivery_failed_at:
            return format_html(
                '<span style="color: red;">Undeliverable ({})</span>',
                obj.get_email_delivery_error_display(),
            )
        return format_html('<span style="color: gray;">Pending</span>')

    def _redeliver(self, obj: t.Any) -> None:
        """Re-dispatch the delivery task for ``obj``. Implemented per model."""
        raise NotImplementedError

    @admin.action(description="Retry delivery (clear undeliverable state)", permissions=["change"])
    def retry_delivery(self, request: HttpRequest, queryset: QuerySet[t.Any]) -> None:
        count = 0
        for obj in queryset.filter(email_delivery_failed_at__isnull=False):
            # clear_email_undeliverable() uses _base_manager.update() (no full_clean), so an
            # org_deleted document's null org FK doesn't block the clear.
            obj.clear_email_undeliverable()
            # Dispatch only after the surrounding ATOMIC_REQUESTS transaction commits, so the
            # worker can't read the still-flagged row before the cleared state lands.
            transaction.on_commit(functools.partial(self._redeliver, obj))
            count += 1
        self.message_user(request, f"Re-dispatched delivery for {count} document(s).", messages.SUCCESS)  # type: ignore[attr-defined]


class CreatedByDefaultMixin:
    """Attribute an admin-created row to the admin when ``created_by`` is left empty.

    ``created_by`` is ``blank=True`` so the row stays valid after its creator is deleted (SET_NULL).
    That must not let a new row be saved without a creator through the admin.
    """

    def save_model(self, request: HttpRequest, obj: t.Any, form: t.Any, change: bool) -> None:
        if not change and obj.created_by_id is None:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)  # type: ignore[misc]


class EventLinkMixin:
    """Mixin to add a link to an event."""

    def event_link(self, obj: t.Any) -> str | None:
        if not hasattr(obj, "event") or not obj.event:
            return None
        url = reverse("admin:events_event_change", args=[obj.event.id])
        return format_html('<a href="{}">{}</a>', url, obj.event.name)

    event_link.short_description = "Event"  # type: ignore[attr-defined]


class VenueLinkMixin:
    """Mixin to add a link to a venue."""

    def venue_link(self, obj: t.Any) -> str | None:
        if not hasattr(obj, "venue") or not obj.venue:
            return None
        url = reverse("admin:events_venue_change", args=[obj.venue.id])
        return format_html('<a href="{}">{}</a>', url, obj.venue.name)

    venue_link.short_description = "Venue"  # type: ignore[attr-defined]


# --- Custom Forms ---
class OrganizationStaffForm(forms.ModelForm):  # type: ignore[type-arg]
    """Custom form to manage the 'permissions' JSONField for OrganizationStaff."""

    # Dynamically create form fields for each default permission
    for key in models.PermissionMap.model_fields:
        locals()[f"default_{key}"] = forms.BooleanField(
            label=key.replace("_", " ").title(),
            required=False,
            widget=forms.CheckboxInput(attrs={"class": CHECKBOX_CLASSES}),
        )

    class Meta:
        model = models.OrganizationStaff
        fields = "__all__"

    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Overriding init."""
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            # Populate form fields from the instance's permissions JSON
            permissions = self.instance.permissions or {}
            default_perms = permissions.get("default", {})
            for key in models.PermissionMap.model_fields:
                self.fields[f"default_{key}"].initial = default_perms.get(key, False)

    def save(self, commit: bool = True) -> models.OrganizationStaff:
        # Reconstruct the 'permissions' dictionary before saving
        default_perms = {
            key: self.cleaned_data.get(f"default_{key}", False) for key in models.PermissionMap.model_fields
        }
        # Keep existing event_overrides, as we don't manage them in this form
        current_overrides = (self.instance.permissions or {}).get("event_overrides", {})

        self.instance.permissions = {
            "default": default_perms,
            "event_overrides": current_overrides,
        }
        return t.cast(models.OrganizationStaff, super().save(commit))


# --- Inlines ---
class OrganizationStaffInline(TabularInline):  # type: ignore[misc]
    model = models.OrganizationStaff
    form = OrganizationStaffForm
    extra = 1
    autocomplete_fields = ["user"]
    fields = ["user"] + [f"default_{key}" for key in models.PermissionMap.model_fields]


class OrganizationMemberInline(TabularInline):  # type: ignore[misc]
    model = models.OrganizationMember
    extra = 1
    autocomplete_fields = ["user", "tier"]
    fields = ["user", "status", "tier"]


class EventSeriesInline(TabularInline):  # type: ignore[misc]
    model = models.EventSeries
    extra = 1
    show_change_link = True
    # No inline delete: it would call ``obj.delete()`` and skip ``event_series_service.delete_event_series`` (#1499).
    can_delete = False
    fields = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}


class OrganizationQuestionnaireInline(TabularInline):  # type: ignore[misc]
    model = models.OrganizationQuestionnaire
    extra = 1
    autocomplete_fields = ["questionnaire"]
    filter_horizontal = ["events", "event_series"]


class MembershipTierInline(TabularInline):  # type: ignore[misc]
    model = models.MembershipTier
    extra = 1
    fields = ["name", "kind", "description"]
    readonly_fields = ["kind"]  # immutable; inline-created tiers are always STANDARD (the model default)


class VenueInline(TabularInline):  # type: ignore[misc]
    model = models.Venue
    extra = 0
    show_change_link = True
    fields = ["name", "slug", "capacity"]
    prepopulated_fields = {"slug": ("name",)}


class VenueSectorInline(TabularInline):  # type: ignore[misc]
    model = models.VenueSector
    extra = 1
    fields = ["name", "code", "capacity", "display_order"]
    ordering = ["display_order", "name"]


class VenueSeatInline(TabularInline):  # type: ignore[misc]
    model = models.VenueSeat
    extra = 1
    fields = ["label", "row_label", "number", "is_accessible", "is_obstructed_view", "is_active"]
    ordering = ["row_order", "adjacency_index", "label"]


class TicketTierFormSet(SaveEditedFieldsFormSetMixin, forms.BaseInlineFormSet):  # type: ignore[type-arg]
    """Rejects new tiers when the parent Event is RSVP-only, incl. on the add page (#1295)."""

    def clean(self) -> None:
        super().clean()
        if self.instance.requires_ticket:
            return
        if any(form.has_changed() and not form.cleaned_data.get("DELETE") for form in self.extra_forms):
            raise ValidationError(_("This event is RSVP-only and does not sell tickets."))


class GrantsTemporaryTierMixin:
    """Offer only TEMPORARY tiers in the ``grants_membership_tier`` picker (the model check yields a single message)."""

    def formfield_for_foreignkey(self, db_field: t.Any, request: HttpRequest, **kwargs: t.Any) -> t.Any:
        if db_field.name == "grants_membership_tier":
            kwargs["queryset"] = models.MembershipTier.objects.filter(kind=models.MembershipTier.Kind.TEMPORARY)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)  # type: ignore[misc]


class TicketTierInline(GrantsTemporaryTierMixin, TabularInline):  # type: ignore[misc]
    model = models.TicketTier
    formset = TicketTierFormSet
    form = EditGuardForm
    extra = 1
    # No inline delete: it would call ``obj.delete()`` and skip ``ticket_service.delete_ticket_tier`` (#1499).
    can_delete = False
    readonly_fields = ["quantity_sold"]  # only the ticket services change the count (#1594)
    # raw_id: plain <select>s here enumerate every venue/sector/membership-tier in the DB
    # and fire one __str__ query per <option>, per row + empty form (#880).
    raw_id_fields = [
        "venue",
        "sector",
        "restricted_to_membership_tiers",
        "excluded_membership_tiers",
        "grants_membership_tier",
    ]

    def has_add_permission(self, request: HttpRequest, obj: models.Event | None = None) -> bool:
        """Block adding tiers to RSVP-only events, which sell nothing (#1295)."""
        if obj is not None and not obj.requires_ticket:
            return False
        return t.cast(bool, super().has_add_permission(request, obj))


class EventInvitationInline(TabularInline):  # type: ignore[misc]
    model = models.EventInvitation
    extra = 1
    autocomplete_fields = ["user"]
    # raw_id: the tiers multi-select enumerates every TicketTier across all events,
    # one Event __str__ query per <option> (#880).
    raw_id_fields = ["tiers"]


class EventRSVPInline(TabularInline):  # type: ignore[misc]
    model = models.EventRSVP
    extra = 1
    autocomplete_fields = ["user"]


class TicketInlineFormSet(SaveEditedFieldsFormSetMixin, forms.BaseInlineFormSet):  # type: ignore[type-arg]
    """Saves an existing ticket with ``update_fields``, so a service write to the status or a check-in is kept."""


class TicketInline(TabularInline):  # type: ignore[misc]
    model = models.Ticket
    formset = TicketInlineFormSet
    form = EditGuardForm
    extra = 0
    autocomplete_fields = ["user"]
    # raw_id: these rendered as plain <select>s over EVERY seat/venue/sector/discount
    # code/held pass in the DB, with one __str__ query per <option>, per ticket row +
    # empty form — thousands of queries on a seated event's change page (#880).
    raw_id_fields = ["venue", "sector", "seat"]
    # Financial snapshots stay as written at purchase, same as on TicketAdmin.
    readonly_fields = [
        "id",
        "checked_in_at",
        "price_paid",
        "discount_amount",
        "offline_refund_amount",
        "refund_policy_snapshot",
        "attribution",
        "sale_source",
        "cancelled_at",
        "cancelled_by",
        "cancellation_source",
        "cancellation_reason",
        "checked_in_by",
        "discount_code",
        "held_pass",
        "file_content_hash",
        # Only the ticket services move a ticket to another tier (#1594). Readonly, not disabled: no unique
        # constraint reads it, and a form field would query the tier again for each row (#880).
        "tier",
    ]
    can_delete = False

    def get_queryset(self, request: HttpRequest) -> QuerySet[models.Ticket]:
        """Load what the readonly columns and ``Ticket.__str__`` show, so a row costs no extra query (#880)."""
        queryset: QuerySet[models.Ticket] = super().get_queryset(request)
        return queryset.select_related(
            "event", "tier__event", "user", "discount_code", "held_pass", "cancelled_by", "checked_in_by"
        )

    def has_add_permission(self, request: HttpRequest, obj: t.Any = None) -> bool:
        """Tickets are created by the checkout and box office services only (#1594)."""
        return False

    def get_formset(self, request: HttpRequest, obj: t.Any = None, **kwargs: t.Any) -> t.Any:
        """Disable status: only the ticket services change it, which keep the tier capacity (#1594).

        Disabled, not readonly, so the form still checks the unique constraints whose condition reads ``status``.
        """
        formset = super().get_formset(request, obj, **kwargs)
        if "status" in formset.form.base_fields:  # a user without change permission gets a form with no fields
            formset.form.base_fields["status"].disabled = True
        return formset


class EventWaitListInline(TabularInline):  # type: ignore[misc]
    model = models.EventWaitList
    extra = 0
    autocomplete_fields = ["user"]
    readonly_fields = ["created_at"]


class WaitlistOfferInline(TabularInline):  # type: ignore[misc]
    """Inline display of waitlist offers on the Event page."""

    model = models.WaitlistOffer
    extra = 0
    fields = ("user", "status", "expires_at", "is_cutoff_batch", "batch_id", "created_at")
    # Without this, `user` rendered as a <select> over every registered user, once per
    # offer row + empty form (#880).
    autocomplete_fields = ["user"]
    readonly_fields = ("created_at", "batch_id")
    show_change_link = True


class SeriesPassTierLinkInline(TabularInline):  # type: ignore[misc]
    model = models.SeriesPassTierLink
    extra = 1
    autocomplete_fields = ["event", "tier"]
    fields = ["event", "tier"]

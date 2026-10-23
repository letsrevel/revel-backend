"""Admin for OrganizationDomain (white-label custom domains).

Status changes go through ``events.service.custom_domain_service`` so the denormalized
``Organization.custom_domain`` and the tenant cache stay in sync; the status itself is therefore
read-only on the form and moved with the actions below. Until Cloudflare automation lands, staff
activate domains by hand here.
"""

import typing as t
from collections.abc import Callable

from django.contrib import admin, messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import QuerySet
from django.http import HttpRequest
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import AutocompleteSelectFilter

from events import models
from events.admin.base import OrganizationLinkMixin
from events.exceptions import CustomDomainNotEnabledError
from events.service import custom_domain_service


@admin.register(models.OrganizationDomain)
class OrganizationDomainAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Admin for custom domains: create a request, then activate / fail / remove it."""

    list_display = ["hostname", "organization_link", "status", "verified_at", "created_at"]
    list_filter = ["status", ("organization", AutocompleteSelectFilter)]
    list_filter_submit = True
    list_select_related = ["organization"]
    search_fields = ["hostname", "organization__name", "organization__slug"]
    autocomplete_fields = ["organization"]
    readonly_fields = ["status", "verified_at", "last_errors", "created_at", "updated_at"]
    actions = ["activate_domains", "mark_domains_failed", "remove_domains"]

    def get_readonly_fields(self, request: HttpRequest, obj: models.OrganizationDomain | None = None) -> list[str]:
        """Lock identity fields once a domain exists; status moves only through the actions."""
        readonly = list(super().get_readonly_fields(request, obj))
        return readonly + ["organization", "hostname"] if obj is not None else readonly

    def has_delete_permission(self, request: HttpRequest, obj: models.OrganizationDomain | None = None) -> bool:
        """Deleting would skip the service's cleanup; use the "Remove" action instead."""
        return False

    def _apply(
        self,
        request: HttpRequest,
        queryset: QuerySet[models.OrganizationDomain],
        transition: Callable[[models.OrganizationDomain], t.Any],
        done: str,
    ) -> None:
        """Run ``transition`` on each selected domain, reporting refusals per row."""
        count = 0
        for domain in queryset.select_related("organization"):
            try:
                transition(domain)
            except CustomDomainNotEnabledError:
                self.message_user(
                    request,
                    f"{domain.hostname}: {custom_domain_service.CUSTOM_DOMAIN_NOT_ENABLED_MESSAGE}",
                    level=messages.ERROR,
                )
            except DjangoValidationError as exc:
                self.message_user(request, f"{domain.hostname}: {'; '.join(exc.messages)}", level=messages.ERROR)
            else:
                count += 1
        if count:
            self.message_user(request, f"{count} domain(s) {done}.", level=messages.SUCCESS)

    @admin.action(description="Activate selected domains", permissions=["change"])
    def activate_domains(self, request: HttpRequest, queryset: QuerySet[models.OrganizationDomain]) -> None:
        """Mark domains ACTIVE and publish them on their organizations."""
        self._apply(request, queryset, custom_domain_service.activate_domain, "activated")

    @admin.action(description="Mark selected domains as failed", permissions=["change"])
    def mark_domains_failed(self, request: HttpRequest, queryset: QuerySet[models.OrganizationDomain]) -> None:
        """Mark domains FAILED and withdraw them from their organizations."""
        self._apply(request, queryset, custom_domain_service.mark_domain_failed, "marked as failed")

    @admin.action(description="Remove selected domains", permissions=["change"])
    def remove_domains(self, request: HttpRequest, queryset: QuerySet[models.OrganizationDomain]) -> None:
        """Mark domains REMOVED, freeing their hostnames."""
        self._apply(request, queryset, custom_domain_service.remove_domain, "removed")

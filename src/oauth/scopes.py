"""Public scope vocabulary and its mapping onto staff ``PermissionKey``s (spec §7.1).

Scopes are the public contract; changing a name or a mapping is a breaking change for
third-party apps and must be reviewed as such.
"""

import typing as t
from dataclasses import dataclass, field

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from oauth2_provider.scopes import BaseScopes

from events.models import PermissionKey

Group = t.Literal["identity", "me", "org"]


@dataclass(frozen=True)
class Scope:
    """One entry of the public scope vocabulary.

    Attributes:
        name: The scope string third-party apps request.
        label: The consent-screen description, shown to the end user.
        group: Which consent-screen section the scope belongs to.
        permission_keys: Staff permission keys this scope unlocks; empty for
            identity and personal scopes, which gate no staff permission.
    """

    name: str
    label: Promise
    group: Group
    permission_keys: frozenset[PermissionKey] = field(default_factory=frozenset)


def _s(name: str, label: Promise, group: Group, *keys: PermissionKey) -> tuple[str, Scope]:
    """Build a ``(name, Scope)`` pair so ``SCOPES`` reads as a flat table."""
    return name, Scope(name=name, label=label, group=group, permission_keys=frozenset(keys))


SCOPES: dict[str, Scope] = dict(
    [
        _s("openid", _("Sign you in"), "identity"),
        _s("profile", _("See your name and picture"), "identity"),
        _s("email", _("See your email address"), "identity"),
        _s("offline_access", _("Stay connected"), "identity"),
        _s("me:read", _("See your profile, tickets, RSVPs, memberships, invoices and payments"), "me"),
        # ``view_organization_details`` is keyed by no permission class and no route (it is only
        # the Literal member, the ``PermissionMap`` default ``True`` and the seeder), so the
        # mapping exists to satisfy the "every key mapped or explicitly unscoped" contract.
        # ``org:read`` is therefore enforced solely by ``RequireScope`` on the switched
        # controllers, never by ``scope_allows()``.
        _s("org:read", _("See your organizations, events and settings"), "org", "view_organization_details"),
        _s(
            "org:events",
            _("Create, edit and delete events and event series, send invitations, and see attendee lists"),
            "org",
            "create_event",
            "edit_event",
            "delete_event",
            "open_event",
            "close_event",
            "manage_event",
            "create_event_series",
            "edit_event_series",
            "delete_event_series",
            "invite_to_event",
        ),
        # ``manage_tickets`` is the widest key in the vocabulary: besides tiers and tickets it
        # gates discount codes (organization_admin/discount_codes.py), seating and box-office
        # selling (event_admin/seating.py), and — with no route-level override —
        # Stripe refunds and per-event revenue (event_admin/tickets.py). The label names all of
        # it because consent is the only thing the end user sees. Two routes keyed on
        # ``manage_event`` reach the same money — cancel-with-refunds and the refund preview in
        # event_admin/core.py — and require this scope *in addition* to ``org:events`` for
        # exactly that reason.
        _s(
            "org:tickets",
            _(
                "Manage ticket tiers, tickets, discount codes and seating, issue refunds, "
                "and see attendee details and revenue"
            ),
            "org",
            "manage_tickets",
        ),
        _s("org:checkin", _("Check attendees in"), "org", "check_in_attendees"),
        # ``manage_subscriptions`` gates the whole subscriptions controller: plans, but also the
        # organization's MRR/churn metrics, the membership payment ledger (amounts, member
        # emails, Stripe ids) and recording or refunding a payment. The label names the money
        # for the same reason ``org:tickets`` does above.
        _s(
            "org:members",
            _("Manage members, subscriptions and membership payments, including refunds, and see membership revenue"),
            "org",
            "manage_members",
            "manage_subscriptions",
        ),
        _s("org:announcements", _("Send announcements"), "org", "send_announcements"),
        _s(
            "org:questionnaires",
            _("Manage and evaluate questionnaires"),
            "org",
            "create_questionnaire",
            "edit_questionnaire",
            "delete_questionnaire",
            "evaluate_questionnaire",
        ),
        _s("org:polls", _("Manage polls"), "org", "manage_polls"),
        _s("org:potluck", _("Manage potluck items"), "org", "manage_potluck"),
    ]
)

# Keys deliberately unreachable through any app token in v1 (spec §7.1).
UNSCOPED_KEYS: frozenset[PermissionKey] = frozenset({"edit_organization"})

_KEY_TO_SCOPES: dict[str, frozenset[str]] = {}
for _scope in SCOPES.values():
    for _key in _scope.permission_keys:
        _KEY_TO_SCOPES[_key] = _KEY_TO_SCOPES.get(_key, frozenset()) | {_scope.name}


def scopes_for_key(key: PermissionKey) -> frozenset[str]:
    """Scopes that unlock ``key``; empty for unscoped keys."""
    return _KEY_TO_SCOPES.get(key, frozenset())


class RegistryScopes(BaseScopes):  # type: ignore[misc]
    """DOT scopes backend fed from ``SCOPES``; the single place per-app restriction lives."""

    def get_all_scopes(self) -> dict[str, str]:
        """Every known scope mapped to its consent-screen label."""
        return {name: str(scope.label) for name, scope in SCOPES.items()}

    def get_available_scopes(
        self, application: t.Any = None, request: t.Any = None, *args: t.Any, **kwargs: t.Any
    ) -> list[str]:
        """Scopes ``application`` may request, or the whole vocabulary when it is unknown."""
        # ponytail: this one method serves two callers with opposite defaults — discovery
        # (DOT's metadata/OIDC views, which legitimately advertise the full vocabulary and pass
        # no application) and restriction (the validator, which always passes a client). The
        # ``None`` branch therefore fails OPEN, and is safe only because every enforcement
        # caller supplies an application. It exists for discovery metadata and must never
        # become the restriction path. If a restriction caller ever has to tolerate a missing
        # application, split this into ``get_available_scopes`` (restrict, deny by default) and
        # a separate ``all_scope_names()`` for metadata rather than widening this branch.
        if application is None:
            return list(SCOPES)
        return [s for s in application.allowed_scopes if s in SCOPES]

    def get_default_scopes(
        self, application: t.Any = None, request: t.Any = None, *args: t.Any, **kwargs: t.Any
    ) -> list[str]:
        """No implicit grants: every scope must be requested explicitly."""
        return []

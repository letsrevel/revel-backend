"""Guard against inline membership status filters and checks (#1315, hardened in #1325).

Membership visibility/access rules live in ``events/utils/membership_access.py``. This AST walk
(no imports, no DB) fails when new code re-introduces an inline status filter, manager-method call or
Python-side status comparison outside the allowlists. One entry is reported per offending AST node.

Rules for ``filter``/``exclude``/``Q(...)`` calls (``create``/``update``/``get_or_create`` etc. and plain
string literals such as GDPR export keys are ignored). ``**{"k": v}`` literals count as keywords:

* (a) a keyword ending (``(^|__)...$``) in ``memberships__status`` / ``organization_memberships__status``
  or ``members`` / ``members__id`` / ``members__pk``, each optionally followed by ``__in``, ``__exact``
  or ``__iexact`` (so ``organization__members__id`` and ``memberships__status__exact`` count);
* (b) ``filter``/``exclude`` on a member receiver (``OrganizationMember.objects`` / ``._default_manager``
  or a related manager ``memberships`` / ``organization_memberships`` / ``members``) with a ``status``,
  ``status__in``, ``status__exact`` or ``status__iexact`` keyword (directly or inside a positional
  ``Q(...)`` expression), or a non-literal ``**mapping``;
* (c) a ``MembershipStatus.<X>`` attribute used as an argument of a ``filter``/``exclude``/``Q`` call.

Other rules:

* (d) any ``.active_only()`` / ``.for_visibility()`` / ``.banned_only()`` / ``.non_cancelled()`` call
  (any receiver): use the ``membership_access`` builders. Allowlist: ``MANAGER_METHOD_ALLOWLIST``.
* (e) a Python-side comparison where an operand is a ``MembershipStatus.<X>`` attribute (or a
  tuple/list/set of them): use the ``is_*_status`` helpers. Allowlist: ``COMPARE_ALLOWLIST``
  (membership/subscription state machines).

``ALLOWLIST`` applies to every rule; entries are single files with reasons, never directories.

Scope limits: querysets held in a variable before ``.filter()``, ``getattr``/dynamic attribute tricks,
and statuses imported under another name are not detected.
"""

import ast
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]

_FILTER_CALLS = {"filter", "exclude"}
_LOOKUP = r"(__(in|exact|iexact))?"
_A_KEYWORD = re.compile(rf"(^|__)((organization_)?memberships__status{_LOOKUP}|members(__(id|pk))?{_LOOKUP})$")
_B_KEYWORDS = {"status", "status__in", "status__exact", "status__iexact"}
_RELATED_MANAGERS = {"memberships", "organization_memberships", "members"}
_MANAGER_METHODS = {"active_only", "for_visibility", "banned_only", "non_cancelled"}

# Paths relative to src/ (prefix match). Each entry carries its reason. Applies to every rule.
ALLOWLIST: tuple[str, ...] = (
    "events/utils/membership_access.py",  # the predicates themselves
    "events/service/organization_service/membership.py",  # membership CRUD: manages status directly
    "events/service/permission_snapshot.py",  # snapshot builder needs raw per-status rows
    "events/service/blacklist_service.py",  # blacklist matching works on raw membership rows
)
# Rule (d) only.
MANAGER_METHOD_ALLOWLIST: tuple[str, ...] = (
    "events/models/membership.py",  # defines the manager/queryset methods
)
# Rule (e) only: membership/subscription state machines compare raw status on purpose.
COMPARE_ALLOWLIST: tuple[str, ...] = (
    "events/signals.py",  # subscription -> member sync: PAUSED/BANNED guards and PAUSED -> ACTIVE resume rule
    "events/service/membership_manager/gates.py",  # PAUSED blocks free apply regardless of tier
    "events/service/membership_manager/service.py",  # PAUSED must not be self-cleared via free apply
    "events/service/subscription/stripe/sync.py",  # CANCELLED -> ACTIVE reactivation on subscription activation
    "events/service/subscription/uncancel.py",  # PAUSED/BANNED members block uncancel (no access helper exists)
)


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _is_member_model(node: ast.expr) -> bool:
    return (isinstance(node, ast.Name) and node.id == "OrganizationMember") or (
        isinstance(node, ast.Attribute) and node.attr == "OrganizationMember"
    )


def _is_member_receiver(node: ast.AST) -> bool:
    """True when the receiver chain goes through OrganizationMember's manager or a related manager."""
    for n in ast.walk(node):
        if not isinstance(n, ast.Attribute):
            continue
        if n.attr in _RELATED_MANAGERS or (n.attr in {"objects", "_default_manager"} and _is_member_model(n.value)):
            return True
    return False


def _is_membership_status_attr(node: ast.AST) -> bool:
    if not isinstance(node, ast.Attribute):
        return False
    owner = node.value
    return (isinstance(owner, ast.Name) and owner.id == "MembershipStatus") or (
        isinstance(owner, ast.Attribute) and owner.attr == "MembershipStatus"
    )


def _keyword_names(node: ast.Call) -> tuple[list[str], bool]:
    """Keyword names (``**{"k": v}`` literal keys included) and whether a non-literal ``**x`` is present."""
    names: list[str] = []
    opaque = False
    for kw in node.keywords:
        if kw.arg is not None:
            names.append(kw.arg)
        elif isinstance(kw.value, ast.Dict) and all(
            isinstance(k, ast.Constant) and isinstance(k.value, str) for k in kw.value.keys
        ):
            names.extend(str(k.value) for k in kw.value.keys if isinstance(k, ast.Constant))
        else:
            opaque = True
    return names, opaque


def _call_flagged(node: ast.Call, name: str) -> bool:
    if name in _MANAGER_METHODS and isinstance(node.func, ast.Attribute):
        return True  # rule (d)
    is_filter = name in _FILTER_CALLS
    if not (is_filter or name == "Q"):
        return False
    names, opaque = _keyword_names(node)
    if any(_A_KEYWORD.search(n) for n in names):  # rule (a)
        return True
    if is_filter and isinstance(node.func, ast.Attribute) and _is_member_receiver(node.func.value):  # rule (b)
        for q in (n for arg in node.args for n in ast.walk(arg)):  # positional Q(...) & ~Q(...) trees
            if isinstance(q, ast.Call) and _call_name(q) == "Q":
                q_names, q_opaque = _keyword_names(q)
                names.extend(q_names)
                opaque = opaque or q_opaque
        if opaque or any(n in _B_KEYWORDS for n in names):
            return True
    arg_nodes: list[ast.AST] = [*node.args, *(kw.value for kw in node.keywords)]
    return any(_is_membership_status_attr(n) for arg in arg_nodes for n in ast.walk(arg))  # rule (c)


def _compare_flagged(node: ast.Compare) -> bool:
    """Rule (e): an operand is a ``MembershipStatus.<X>`` attribute or a tuple/list/set of them."""
    for operand in (node.left, *node.comparators):
        if _is_membership_status_attr(operand):
            return True
        if isinstance(operand, (ast.Tuple, ast.List, ast.Set)) and any(
            _is_membership_status_attr(e) for e in operand.elts
        ):
            return True
    return False


def find_violations(source: str, path: str) -> list[str]:
    """Return ``path:line`` entries for every inline membership status use in ``source``."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Call, ast.Compare)):
            continue
        flagged = False
        if isinstance(node, ast.Call) and (name := _call_name(node)) is not None:
            flagged = _call_flagged(node, name)
            if name in _MANAGER_METHODS and path.startswith(MANAGER_METHOD_ALLOWLIST):
                flagged = False
        elif isinstance(node, ast.Compare):
            flagged = _compare_flagged(node) and not path.startswith(COMPARE_ALLOWLIST)
        if flagged:
            hits.append(f"{path}:{node.lineno}")
    return hits


def test_guard_detects_violation() -> None:
    """Each rule and each hardened shape is caught exactly once."""
    snippets = [
        "Event.objects.filter(organization_memberships__status='active')",  # (a)
        "OrganizationMember.objects.filter(user=u, status__in=statuses)",  # (b)
        "Q(user=u) & Q(role=OrganizationMember.MembershipStatus.ACTIVE)",  # (c)
        "Event.objects.filter(organization__memberships__status__in=s)",  # (a) join from Event
        "Ticket.objects.filter(user__organization_memberships__status=s)",  # (a) join from Ticket
        "Event.objects.filter(organization__members__id=1)",  # (a) members__id
        "models.OrganizationMember.objects.filter(status=s)",  # (b) attribute-qualified model
        "Organization.objects.filter(members=x)",  # (a) bare M2M
        "Event.objects.filter(organization__members=x)",  # (a) bare M2M join
        "Organization.objects.filter(members__in=x)",  # (a) bare M2M __in
        "Organization.objects.filter(members__pk=x)",  # (a) bare M2M __pk
        "Event.objects.filter(organization__memberships__status__exact=s)",  # (a) lookup suffix
        "Ticket.objects.filter(user__organization_memberships__status__iexact=s)",  # (a) lookup suffix
        "Event.objects.filter(organization__members__id__in=ids)",  # (a) lookup suffix
        "Organization.objects.filter(members__pk__exact=x)",  # (a) lookup suffix
        "org.memberships.filter(status=s)",  # (b) related manager
        "user.organization_memberships.exclude(status__in=s)",  # (b) related manager
        "OrganizationMember._default_manager.filter(status=s)",  # (b) _default_manager
        'OrganizationMember.objects.filter(**{"status": s})',  # (b) literal unpacking
        "OrganizationMember.objects.filter(**lookups)",  # (b) opaque unpacking
        "OrganizationMember.objects.filter(Q(user=u) & ~Q(status=s))",  # (b) positional Q
        "org.memberships.exclude(Q(status__in=s) | Q(tier=t))",  # (b) positional Q, related manager
        "OrganizationMember.objects.active_only()",  # (d)
        "qs.for_visibility()",  # (d)
        "x.banned_only()",  # (d)
        "x.non_cancelled()",  # (d)
        "m.status == OrganizationMember.MembershipStatus.ACTIVE",  # (e)
        "MembershipStatus.PAUSED != m.status",  # (e)
        "m.status in (OrganizationMember.MembershipStatus.ACTIVE, OrganizationMember.MembershipStatus.PAUSED)",  # (e)
    ]
    assert [len(find_violations(s, "x.py")) for s in snippets] == [1] * len(snippets)


def test_guard_ignores_unrelated() -> None:
    """Other models, non-member M2Ms and unrelated comparisons are not flagged."""
    snippets = [
        "Organization.objects.filter(staff_members__id=1)",
        "Ticket.objects.filter(status=s)",
        "org.staff_members.filter(status=s)",
        "ticket.status == Ticket.TicketStatus.ACTIVE",
        "OrganizationMembershipRequest.objects.filter(status=s)",
        "self.members_count == 3",
    ]
    assert [find_violations(s, "x.py") for s in snippets] == [[]] * len(snippets)


def test_guard_per_rule_allowlists() -> None:
    """Rule (d) and (e) allowlists exempt only their own rule."""
    assert find_violations("x.active_only()", "events/models/membership.py") == []
    assert len(find_violations("Q(status=MembershipStatus.ACTIVE)", "events/models/membership.py")) == 1
    for entry in COMPARE_ALLOWLIST:
        assert find_violations("m.status == MembershipStatus.ACTIVE", entry) == []


def test_guard_ignores_gdpr_string_keys() -> None:
    """Plain strings / dict keys / create calls are not filters."""
    source = (
        'RULES = {"organization_memberships": 1, "member_organizations": 2}\n'
        'x = "organization_memberships"\n'
        "OrganizationMember.objects.create(status=OrganizationMember.MembershipStatus.ACTIVE)\n"
    )
    assert find_violations(source, "x.py") == []


def test_no_inline_membership_status_filters() -> None:
    """No production module outside the allowlist filters on membership status inline."""
    violations: list[str] = []
    scanned = 0
    for file in sorted(_SRC.rglob("*.py")):
        rel = file.relative_to(_SRC).as_posix()
        parts = file.relative_to(_SRC).parts
        if "tests" in parts or "migrations" in parts or rel.startswith(ALLOWLIST):
            continue
        scanned += 1
        violations.extend(find_violations(file.read_text(), rel))
    assert scanned > 100, f"guard scanned only {scanned} files; the walk is probably broken"
    assert not violations, "Inline membership status filters (use events.utils.membership_access):\n" + "\n".join(
        violations
    )

"""Privacy utils (GDPR Art. 15 data export).

Export policy is an explicit **allowlist**: every reverse relation on
``RevelUser`` must have an entry in :data:`EXPORT_RULES` — either included
(optionally with a custom serializer / field excludes) or excluded with a
documented reason. Unmapped relations are skipped at runtime (default-deny)
and rejected by the registry guard test, so a new model with a user FK forces
an explicit export decision instead of silently joining the export (#798).

Two leak classes drove this design:

* **Actor relations** — rows where the user is the acting staff member/admin
  (``decided_by``, ``checked_in_by``, ``recorded_by``, evaluator, …) are about
  *other* data subjects and must never appear in this user's export.
* **Business internals** — M2M expansion of organizations dumped VAT/billing/
  fee data to mere members; those are now reduced to id/name/slug.
"""

import dataclasses
import io
import typing as t
import zipfile
from uuid import UUID

import orjson
import structlog
from django.contrib.gis.geos import Point
from django.core.files.base import ContentFile
from django.db.models import ManyToManyRel, ManyToOneRel, Model, OneToOneRel
from django.db.models.fields.files import FieldFile
from django.forms.models import model_to_dict
from django.utils import timezone

from accounts.models import ReferralPayout, ReferralPayoutStatement, RevelUser, UserDataExport
from common.signing import generate_signed_url, is_protected_path
from questionnaires.models import (
    FreeTextAnswer,
    MultipleChoiceAnswer,
    QuestionnaireSubmission,
)

logger = structlog.get_logger(__name__)

# Signed URLs inside the export share the lifetime of the export download link
# (7 days). Single source of truth: accounts/tasks/gdpr.py aliases this as
# DATA_EXPORT_URL_EXPIRES_IN, so the two can never drift.
EXPORT_FILE_URL_EXPIRES_IN = 7 * 24 * 60 * 60

_EXPORT_FALLBACK = "[This field could not be exported]"


def _sanitize_dict_keys(data: t.Any) -> t.Any:
    """Recursively convert all dict keys to strings for orjson compatibility.

    orjson requires all dict keys to be strings. Django's model_to_dict can return
    non-string keys (e.g., FK ids), so we need to stringify them.

    Args:
        data: Data structure to sanitize

    Returns:
        Data with all dict keys as strings
    """
    if isinstance(data, dict):
        return {str(k): _sanitize_dict_keys(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_sanitize_dict_keys(item) for item in data]
    return data


def _file_url(field_file: FieldFile) -> str | None:
    """URL for a file field — signed (7 days) for protected paths.

    Raw ``.url`` on a protected path is a dead link (the file server requires
    a signature), so exports sign it for the same lifetime as the export
    download link.
    """
    if not field_file:
        return None
    name = getattr(field_file, "name", None)
    if isinstance(name, str) and is_protected_path(name):
        return generate_signed_url(name, expires_in=EXPORT_FILE_URL_EXPIRES_IN)
    return field_file.url


def _default_serializer(obj: t.Any) -> t.Any:
    """Handle Django-specific types for orjson serialization.

    orjson natively handles: str, int, float, bool, None, dict, list, tuple,
    datetime, date, time, UUID, bytes, and more.

    This function only needs to handle Django-specific types that orjson
    doesn't know about.

    Args:
        obj: Object to serialize

    Returns:
        JSON-serializable representation

    Raises:
        TypeError: If object type cannot be serialized (required by orjson)
    """
    # Handle FileField/ImageField - convert to (signed) URL
    if isinstance(obj, FieldFile):
        try:
            url = _file_url(obj)
            if url is not None:
                return url
        except Exception:
            # File doesn't exist or storage issue
            logger.debug("gdpr_export_file_url_failed", field_name=getattr(obj, "name", None))
        return _EXPORT_FALLBACK

    # Handle PostGIS Point - convert to GeoJSON
    if isinstance(obj, Point):
        return {
            "type": "Point",
            "coordinates": [obj.x, obj.y],
        }

    # Fallback: try string representation
    try:
        return str(obj)
    except Exception:
        # If even str() fails, use generic fallback
        # We must raise TypeError to tell orjson we can't handle this,
        # but we want to be permissive for GDPR exports
        logger.warning(
            "gdpr_export_field_serialization_fallback",
            object_type=type(obj).__name__,
        )
        return _EXPORT_FALLBACK


def _dump(obj: Model, exclude: tuple[str, ...] = ()) -> dict[str, t.Any]:
    """Dump a model row for export: ``model_to_dict`` minus excludes, plus pk/timestamps.

    ``model_to_dict`` drops ``editable=False`` fields, which strips the pk and
    ``created_at``/``updated_at`` from every row — an Art. 12(1)
    intelligibility problem. So this re-adds ``id`` (the pk) unconditionally,
    and ``created_at``/``updated_at`` when the model defines them.

    Args:
        obj: The model instance to dump.
        exclude: Field names to drop from the ``model_to_dict`` output.

    Returns:
        The row as a plain dict, ready for orjson.
    """
    data = {k: v for k, v in model_to_dict(obj).items() if k not in exclude}
    data["id"] = obj.pk
    for ts_field in ("created_at", "updated_at"):
        if hasattr(obj, ts_field):
            data[ts_field] = getattr(obj, ts_field)
    return data


# ---------------------------------------------------------------------------
# Custom per-relation serializers (value only; the registry supplies the key)
# ---------------------------------------------------------------------------


def _serialize_org_summaries(user: RevelUser, accessor: str) -> list[dict[str, t.Any]]:
    """Organizations the user belongs to, reduced to identity fields.

    Full ``Organization`` rows carry VAT/billing/fee/Stripe data that belongs
    to the business, not to the member — the membership itself is exported via
    the user's own ``OrganizationMember``/``OrganizationStaff`` rows.
    """
    return [{"id": org.pk, "name": org.name, "slug": org.slug} for org in getattr(user, accessor).all()]


def _serialize_owned_organizations(user: RevelUser) -> list[dict[str, t.Any]]:
    """Organizations the user owns, minus the member/staff rosters.

    The owner's org business data is their own (sole-trader case), but the
    members/staff M2M pk lists are other people's identifiers.
    """
    return [_dump(org, exclude=("members", "staff_members")) for org in user.owned_organizations.all()]


def _serialize_waitlisted_events(user: RevelUser) -> list[dict[str, t.Any]]:
    """Events the user is waitlisted for, reduced to identity fields."""
    return [
        {"id": event.pk, "name": event.name, "slug": event.slug, "start": event.start} for event in user.waitlist.all()
    ]


def _serialize_referral(user: RevelUser) -> dict[str, t.Any] | None:
    """The referral that brought this user in (code string, not user ids)."""
    referral = getattr(user, "referral", None)
    if referral is None:
        return None
    return {
        "referral_code": referral.referral_code.code,
        "revenue_share_percent": referral.revenue_share_percent,
        "created_at": referral.created_at,
    }


def _serialize_referrals_made(user: RevelUser) -> list[dict[str, t.Any]]:
    """Referrals generated by the user's code — without the referred users' ids."""
    return [
        {"revenue_share_percent": referral.revenue_share_percent, "created_at": referral.created_at}
        for referral in user.referrals_made.all()
    ]


def _serialize_referral_payouts(user: RevelUser) -> list[dict[str, t.Any]]:
    """Payouts earned on the user's referrals, without the referral's user ids."""
    return [
        {
            "period_start": payout.period_start,
            "period_end": payout.period_end,
            "net_platform_fees": payout.net_platform_fees,
            "payout_amount": payout.payout_amount,
            "rolled_over_amount": payout.rolled_over_amount,
            "currency": payout.currency,
            "status": payout.status,
            "stripe_transfer_id": payout.stripe_transfer_id,
            "created_at": payout.created_at,
        }
        for payout in ReferralPayout.objects.filter(referral__referrer=user)
    ]


def _serialize_referral_payout_statements(user: RevelUser) -> list[dict[str, t.Any]]:
    """Per-payout statement lines for the user's referral payouts (depth 2)."""
    statements = ReferralPayoutStatement.objects.filter(payout__referral__referrer=user)
    return [_dump(statement, exclude=("payout",)) for statement in statements]


def _serialize_attendee_invoice_credit_notes(user: RevelUser) -> list[dict[str, t.Any]]:
    """Credit notes issued against the user's own invoices (depth 2)."""
    from events.models import AttendeeInvoiceCreditNote

    return [_dump(note) for note in AttendeeInvoiceCreditNote.objects.filter(invoice__user=user)]


def _serialize_membership_payments(user: RevelUser) -> list[dict[str, t.Any]]:
    """Payments recorded against the user's own membership subscriptions (depth 2).

    ``recorded_by`` is the staff member who booked the payment — third-party
    data — so it is stripped; ``subscription`` is kept because it links back to
    the user's exported ``membership_subscriptions`` rows.
    """
    from events.models import MembershipPayment

    return [
        _dump(payment, exclude=("recorded_by",))
        for payment in MembershipPayment.objects.filter(subscription__user=user)
    ]


def _serialize_dietary_restrictions(user: RevelUser) -> list[dict[str, t.Any]]:
    """Serialize dietary restrictions with expanded food_item details.

    Args:
        user: The user whose restrictions to serialize

    Returns:
        List of restrictions with food_item name included
    """
    restrictions = user.dietary_restrictions.select_related("food_item").all()
    return [
        {
            "food_item_name": restriction.food_item.name,
            "restriction_type": restriction.restriction_type,
            "notes": restriction.notes,
            "is_public": restriction.is_public,
            "created_at": restriction.created_at,
        }
        for restriction in restrictions
    ]


def _serialize_dietary_preferences(user: RevelUser) -> list[dict[str, t.Any]]:
    """Serialize dietary preferences with expanded preference details.

    Args:
        user: The user whose preferences to serialize

    Returns:
        List of preferences with preference name included
    """
    preferences = user.dietary_preferences.select_related("preference").all()
    return [
        {
            "preference_name": pref.preference.name,
            "comment": pref.comment,
            "is_public": pref.is_public,
            "created_at": pref.created_at,
        }
        for pref in preferences
    ]


def _serialize_questionnaire_data(user_id: UUID) -> dict[str, list[dict[str, t.Any]]]:
    """Special case serializer for detailed questionnaire data."""
    submissions = QuestionnaireSubmission.objects.filter(user_id=user_id).select_related("questionnaire", "evaluation")
    data = []
    for sub in submissions:
        sub_data: dict[str, t.Any] = {
            "submission_id": sub.id,
            "questionnaire_name": sub.questionnaire.name,
            "status": sub.status,
            "submitted_at": sub.submitted_at,
            "evaluation": None,
            "answers": [],
        }
        if hasattr(sub, "evaluation") and sub.evaluation:
            sub_data["evaluation"] = {
                "status": sub.evaluation.status,
                "score": sub.evaluation.score,
                "comments": sub.evaluation.comments,
            }

        answers: list[dict[str, t.Any]] = []
        mc_answers = MultipleChoiceAnswer.objects.filter(submission=sub).select_related("question", "option")
        for mc_ans in mc_answers:
            answers.append(
                {
                    "type": "multiple_choice",
                    "question": mc_ans.question.question,
                    "answer": mc_ans.option.option,
                }
            )

        ft_answers = FreeTextAnswer.objects.filter(submission=sub).select_related("question")
        for ft_ans in ft_answers:
            answers.append(
                {
                    "type": "free_text",
                    "question": ft_ans.question.question,
                    "answer": ft_ans.answer,
                }
            )

        sub_data["answers"] = answers
        data.append(sub_data)
    return {"questionnaire_submissions": data}


# ---------------------------------------------------------------------------
# Export registry
# ---------------------------------------------------------------------------

_Serializer = t.Callable[[RevelUser], t.Any]


@dataclasses.dataclass(frozen=True)
class ExportRule:
    """Export decision for one reverse relation on ``RevelUser``.

    * ``include=False`` — never exported; ``reason`` documents why.
    * ``include=True, serializer=None`` — generic ``_dump`` (minus
      ``exclude_fields``); single object for one-to-one relations, list
      otherwise.
    * ``include=True, serializer=...`` — callable receives the user and
      returns the exported value.
    """

    include: bool
    reason: str = ""
    serializer: _Serializer | None = None
    exclude_fields: tuple[str, ...] = ()


_EXCLUDED_THIRD_PARTY = "third-party data handled in a staff/admin capacity"
_EXCLUDED_MODERATION = "moderation/fraud-prevention record — excluded pending policy decision (#798)"

EXPORT_RULES: dict[str, ExportRule] = {
    # --- accounts ---
    "dietary_restrictions": ExportRule(include=True, serializer=_serialize_dietary_restrictions),
    "dietary_preferences": ExportRule(include=True, serializer=_serialize_dietary_preferences),
    "verification_reminder_tracking": ExportRule(include=True),
    "global_bans": ExportRule(include=False, reason=_EXCLUDED_MODERATION),
    # Transparency: *that* the user was impersonated and when — not the admin's
    # identity/IP/user-agent (third-party staff data) nor the token id.
    "impersonations_received": ExportRule(
        include=True, exclude_fields=("admin_user", "token_jti", "ip_address", "user_agent")
    ),
    "impersonations_performed": ExportRule(include=False, reason=_EXCLUDED_THIRD_PARTY),
    "referral": ExportRule(include=True, serializer=_serialize_referral),
    "referrals_made": ExportRule(include=True, serializer=_serialize_referrals_made),
    "referral_code": ExportRule(include=True),
    "billing_profile": ExportRule(include=True),
    "data_export": ExportRule(include=False, reason="export bookkeeping (self-referential)"),
    # --- django / third-party apps ---
    "logentry_set": ExportRule(include=False, reason="admin audit log about arbitrary records"),
    "outstandingtoken_set": ExportRule(include=False, reason="JWT session secrets"),
    "googlessouser": ExportRule(include=True),
    # --- common ---
    "file_exports": ExportRule(include=False, reason="internal operational record"),
    # --- events: data-subject rows ---
    "tickets": ExportRule(include=True),
    "payments": ExportRule(include=True),
    "rsvps": ExportRule(include=True),
    "invitations": ExportRule(include=True),
    "event_questionnaire_submissions": ExportRule(include=True),
    "event_bookmarks": ExportRule(include=True),
    "event_series_follows": ExportRule(include=True),
    "organization_follows": ExportRule(include=True),
    "organization_memberships": ExportRule(include=True),
    "organization_staff_memberships": ExportRule(include=True),
    "eventwaitlist_set": ExportRule(include=True),
    "waitlist_offers": ExportRule(include=True),
    # ``decided_by`` is the deciding staff member's id — third-party data on the
    # requester's own row (``UserRequestMixin``), so it is stripped here too.
    "whitelist_requests": ExportRule(include=True, exclude_fields=("decided_by",)),
    "eventinvitationrequest_set": ExportRule(include=True, exclude_fields=("decided_by",)),
    "organizationmembershiprequest_set": ExportRule(include=True, exclude_fields=("decided_by",)),
    "held_series_passes": ExportRule(include=True),
    "membership_subscriptions": ExportRule(include=True),
    "seat_holds": ExportRule(include=True),
    "attendee_invoices": ExportRule(include=True),
    "sent_contact_messages": ExportRule(include=True),
    "general_preferences": ExportRule(include=True),
    "potluck_items": ExportRule(include=True, exclude_fields=("created_by",)),
    "potluckitem_set": ExportRule(include=True, exclude_fields=("assignee",)),
    # --- events: reduced shapes ---
    "owned_organizations": ExportRule(include=True, serializer=_serialize_owned_organizations),
    "member_organizations": ExportRule(
        include=True, serializer=lambda user: _serialize_org_summaries(user, "member_organizations")
    ),
    "staff_organizations": ExportRule(
        include=True, serializer=lambda user: _serialize_org_summaries(user, "staff_organizations")
    ),
    "waitlist": ExportRule(include=True, serializer=_serialize_waitlisted_events),
    # --- events: staff/actor and operational rows ---
    "eventinvitationrequest_decided_by": ExportRule(include=False, reason=_EXCLUDED_THIRD_PARTY),
    "organizationmembershiprequest_decided_by": ExportRule(include=False, reason=_EXCLUDED_THIRD_PARTY),
    "checked_in_tickets": ExportRule(include=False, reason=_EXCLUDED_THIRD_PARTY),
    "cancelled_tickets": ExportRule(include=False, reason=_EXCLUDED_THIRD_PARTY),
    "recorded_membership_payments": ExportRule(include=False, reason=_EXCLUDED_THIRD_PARTY),
    "eventtoken_tokens": ExportRule(include=False, reason="operational invite tokens (secret ids)"),
    "organizationtoken_tokens": ExportRule(include=False, reason="operational invite tokens (secret ids)"),
    "created_announcements": ExportRule(include=False, reason="organization content authored in staff capacity"),
    "blacklist_entries": ExportRule(include=False, reason=_EXCLUDED_MODERATION),
    "visible_attendees": ExportRule(include=False, reason="cross-user visibility flags (third-party linkage)"),
    "visible_to": ExportRule(include=False, reason="cross-user visibility flags (third-party linkage)"),
    # --- notifications ---
    "notifications": ExportRule(include=False, reason="transient rendered notifications; preferences are exported"),
    "notification_preferences": ExportRule(include=True),
    # --- questionnaires ---
    "questionnaire_submissions": ExportRule(
        include=True, serializer=lambda user: _serialize_questionnaire_data(user.id)["questionnaire_submissions"]
    ),
    "questionnaire_files": ExportRule(include=True),
    "questionnaireevaluation_set": ExportRule(include=False, reason="evaluations authored about other users"),
    # --- telegram ---
    "telegram_users": ExportRule(include=True),
}

# Data about the user that is not a direct reverse relation (depth 2).
EXTRA_SECTIONS: dict[str, _Serializer] = {
    "referral_payouts": _serialize_referral_payouts,
    "referral_payout_statements": _serialize_referral_payout_statements,
    "attendee_invoice_credit_notes": _serialize_attendee_invoice_credit_notes,
    "membership_payments": _serialize_membership_payments,
}


def get_user_reverse_relations() -> dict[str, OneToOneRel | ManyToOneRel | ManyToManyRel]:
    """Map accessor name → reverse relation for every relation on ``RevelUser``."""
    relations: dict[str, OneToOneRel | ManyToOneRel | ManyToManyRel] = {}
    for field in RevelUser._meta.get_fields():
        if isinstance(field, (OneToOneRel, ManyToOneRel, ManyToManyRel)) and field.related_model:
            accessor = field.get_accessor_name()
            if accessor:
                relations[accessor] = field
    return relations


def _serialize_related_objects(user: RevelUser) -> dict[str, t.Any]:
    """Serialize the allowlisted related objects for a user.

    Args:
        user: The user whose related objects to serialize

    Returns:
        Dictionary with all related object data
    """
    export_data: dict[str, t.Any] = {}

    for accessor, rel in get_user_reverse_relations().items():
        rule = EXPORT_RULES.get(accessor)
        if rule is None:
            # Default-deny: a relation without an export decision is skipped.
            # The registry guard test fails first, so this only triggers if a
            # migration lands without its export entry.
            logger.warning("gdpr_export_unmapped_relation", accessor=accessor)
            continue
        if not rule.include:
            continue

        if rule.serializer is not None:
            export_data[accessor] = rule.serializer(user)
            continue

        value = getattr(user, accessor, None)
        if value is None:
            continue
        if isinstance(rel, OneToOneRel):
            export_data[accessor] = _dump(value, exclude=rule.exclude_fields)
        else:
            export_data[accessor] = [_dump(obj, exclude=rule.exclude_fields) for obj in value.all()]

    for section, serializer in EXTRA_SECTIONS.items():
        export_data[section] = serializer(user)

    return export_data


def generate_user_data_export(user: RevelUser) -> UserDataExport:
    """Generate a data export for a user."""
    logger.info("gdpr_export_started", user_id=str(user.id))

    export: UserDataExport | None = None
    try:
        export, created = UserDataExport.objects.get_or_create(user=user)
        if created:
            logger.info("gdpr_export_created", user_id=str(user.id), export_id=str(export.id))

        export.status = UserDataExport.UserDataExportStatus.PROCESSING
        export.save(update_fields=["status"])

        # 1. Serialize user profile fields
        user_fields = {
            f.name: getattr(user, f.name)
            for f in user._meta.fields
            if f.name not in ["password", "totp_secret_encrypted", "totp_secret"]
        }
        export_data = {"profile": user_fields}

        # 2. Serialize related objects
        export_data.update(_serialize_related_objects(user))

        # 3. Sanitize dict keys (orjson requires string keys) and serialize
        sanitized_data = _sanitize_dict_keys(export_data)
        json_bytes = orjson.dumps(
            sanitized_data,
            default=_default_serializer,
            option=orjson.OPT_INDENT_2,  # Pretty print with 2-space indent
        )

        # 4. Create a ZIP archive in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr("revel_user_data.json", json_bytes)

        zip_buffer.seek(0)

        # 5. Save the ZIP to the model's FileField
        export.file.save(f"revel_export_{user.id}.zip", ContentFile(zip_buffer.read()), save=False)
        export.status = UserDataExport.UserDataExportStatus.READY
        export.completed_at = timezone.now()
        export.save(update_fields=["status", "file", "completed_at"])

        logger.info(
            "gdpr_export_completed",
            user_id=str(user.id),
            export_id=str(export.id),
            file_size_bytes=export.file.size,
            data_categories=list(export_data.keys()),
        )
        return export

    except Exception as e:
        logger.error(
            "gdpr_export_failed",
            user_id=str(user.id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        if export:
            export.status = UserDataExport.UserDataExportStatus.FAILED
            export.save(update_fields=["status"])
        raise

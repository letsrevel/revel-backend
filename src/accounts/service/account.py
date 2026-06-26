"""Service layer for the authentication app."""

import typing as t

import jwt
import structlog
from django.conf import settings
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_google_sso.models import GoogleSSOUser
from ninja import Schema
from ninja.errors import HttpError

from accounts import schema, tasks
from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import blacklist_user_tokens, check_blacklist, create_token
from accounts.models import Referral, ReferralCode, RevelUser
from accounts.password_validation import validate_password
from common.testing import (
    TOKEN_TYPE_DELETION,
    TOKEN_TYPE_EMAIL_CHANGE,
    TOKEN_TYPE_PASSWORD_RESET,
    TOKEN_TYPE_VERIFICATION,
    store_test_token,
)

logger = structlog.get_logger(__name__)


def _send_activation_email_for_guest(user: RevelUser) -> None:
    """Send an account activation email (password-reset link) to a guest user.

    Uses a password-reset token so that ``reset_password()`` handles the
    guest-to-full-user conversion when the link is clicked.
    """
    token = create_password_reset_token(user)
    # Bare dispatch (no on_commit): this helper is only invoked on the
    # duplicate-registration anti-enumeration path, which dispatches the
    # activation email and then raises HttpError(400), rolling the request
    # transaction back. The target guest already exists (committed), so there
    # is no read-after-commit race and the email must be sent despite the
    # rollback. See register_user.
    tasks.send_account_email.delay(tasks.AccountEmail.ACTIVATION, user.email, token=token)
    logger.info("account_activation_email_sent", user_id=str(user.id), email=user.email)


@transaction.atomic
def register_user(payload: schema.RegisterUserSchema) -> tuple[RevelUser, str]:
    """Register a new user, and send a verification email.

    If the email belongs to an existing guest user, sends an account activation
    email (password-reset link) instead of converting the account directly.
    This prevents account takeover — only the real email owner can complete
    activation.

    Uses ``select_for_update`` on the existing-user lookup so that concurrent
    registrations for the same email serialise correctly.

    Args:
        payload: The registration data.

    Returns:
        A tuple of the user and the verification token.
    """
    from accounts.service.global_ban_service import BAN_ERROR_MESSAGE, is_email_globally_banned

    logger.info("user_registration_started", email=payload.email)
    if is_email_globally_banned(payload.email):
        raise HttpError(403, str(BAN_ERROR_MESSAGE))
    if existing_user := RevelUser.objects.select_for_update().filter(username=payload.email).first():
        if existing_user.guest:
            _send_activation_email_for_guest(existing_user)
        elif not existing_user.email_verified:
            logger.info("user_registration_duplicate_unverified", email=payload.email)
            send_verification_email_for_user(existing_user, defer=False)
        logger.warning("user_registration_duplicate", email=payload.email)
        raise HttpError(400, str(_("A user with this email already exists.")))

    # Validate referral code after existing-user check but before creating the user
    referral_code_obj: ReferralCode | None = None
    if payload.referral_code is not None:
        referral_code_obj = ReferralCode.objects.filter(code=payload.referral_code.upper(), is_active=True).first()
        if not referral_code_obj:
            raise HttpError(422, str(_("Invalid or inactive referral code.")))

    new_user = RevelUser.objects.create_user(
        username=payload.email,
        email=payload.email,
        password=payload.password1,
        first_name=payload.first_name,
        last_name=payload.last_name,
        is_active=True,  # we use email verification
    )

    if referral_code_obj:
        Referral.objects.create(
            referral_code=referral_code_obj,
            referred_user=new_user,
        )
        logger.info(
            "referral_created",
            user_id=str(new_user.id),
            referrer_id=str(referral_code_obj.user_id),
            code=referral_code_obj.code,
        )

    logger.info("user_registration_completed", user_id=str(new_user.id), email=new_user.email)
    return send_verification_email_for_user(new_user)


def create_verification_token(user: RevelUser) -> str:
    """Create a verification token for a user without sending email.

    Args:
        user (RevelUser): The user to create a token for.

    Returns:
        str: The verification token.
    """
    verification_payload = schema.VerifyEmailJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(verification_payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    store_test_token(TOKEN_TYPE_VERIFICATION, token)
    return token


def create_password_reset_token(user: RevelUser) -> str:
    """Create a password reset token for a user without sending email.

    Args:
        user: The user to create a token for.

    Returns:
        The password reset token.
    """
    payload = schema.PasswordResetJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    store_test_token(TOKEN_TYPE_PASSWORD_RESET, token)
    return token


def create_deletion_token(user: RevelUser) -> str:
    """Create an account deletion token for a user without sending email.

    Args:
        user (RevelUser): The user to create a token for.

    Returns:
        str: The deletion token.
    """
    payload = schema.DeleteAccountJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    store_test_token(TOKEN_TYPE_DELETION, token)
    return token


def send_verification_email_for_user(user: RevelUser, *, defer: bool = True) -> tuple[RevelUser, str]:
    """Send a verification email for a user.

    Args:
        user: The user to email.
        defer: When True (default), the dispatch is deferred until the
            surrounding transaction commits, so a freshly-created user row is
            visible to the worker (avoids a read-after-commit race). Pass
            ``defer=False`` on the duplicate-registration anti-enumeration path
            that dispatches and then rolls the request transaction back: there
            the target user already exists, so the email must be sent
            regardless of the rollback.
    """
    logger.info("verification_email_requested", user_id=str(user.id), email=user.email)
    token = create_verification_token(user)
    if defer:
        transaction.on_commit(
            lambda: tasks.send_account_email.delay(tasks.AccountEmail.VERIFICATION, user.email, token=token)
        )
    else:
        tasks.send_account_email.delay(tasks.AccountEmail.VERIFICATION, user.email, token=token)
    return user, token


@transaction.atomic
def verify_email(token: str) -> RevelUser:
    """Verify a user's email.

    Automatically reactivates deactivated accounts upon verification and clears
    any reminder tracking to prevent further emails.

    Args:
        token (str): The verification token.

    Returns:
        RevelUser: The verified user.
    """
    from accounts.models import EmailVerificationReminderTracking
    from accounts.service.global_ban_service import BAN_ERROR_MESSAGE, is_email_globally_banned

    payload = token_to_payload(token, schema.VerifyEmailJWTPayloadSchema)
    check_blacklist(payload.jti)
    if user := RevelUser.objects.filter(id=payload.user_id).first():
        # Guest users must not be verified through this flow — they were never registered
        if user.guest:
            logger.warning("email_verification_blocked_guest_user", user_id=str(user.id), email=user.email)
            raise HttpError(400, str(_("Invalid verification token.")))
        if is_email_globally_banned(user.email):
            blacklist_token(token)
            raise HttpError(403, str(BAN_ERROR_MESSAGE))
        blacklist_token(token)
        was_inactive = not user.is_active
        user.is_active = user.email_verified = True
        user.save(update_fields=["is_active", "email_verified"])

        # Delete reminder tracking record to stop all reminder emails
        EmailVerificationReminderTracking.objects.filter(user=user).delete()

        logger.info("email_verified", user_id=str(user.id), email=user.email, was_reactivated=was_inactive)
        return user
    logger.warning("email_verification_failed_user_not_found", user_id=str(payload.user_id))
    raise HttpError(400, str(_("A user with this email no longer exists.")))


def resend_verification_email(email: str) -> None:
    """Resend verification email for a user.

    Silently handles all cases to prevent user enumeration:
    - If user doesn't exist: do nothing
    - If email already verified: do nothing
    - If user exists and unverified: send verification email

    Args:
        email (str): The email address of the user.
    """
    logger.info("verification_email_resend_requested", email=email)
    try:
        user = RevelUser.objects.get(username=email)
    except RevelUser.DoesNotExist:
        logger.info("verification_email_resend_user_not_found", email=email)
        return None

    # Guest users must not receive verification emails — they were never registered
    if user.guest:
        logger.warning("verification_email_resend_blocked_guest_user", user_id=str(user.id), email=email)
        return None

    if user.email_verified:
        logger.info("verification_email_resend_already_verified", user_id=str(user.id), email=email)
        return None

    from accounts.service.global_ban_service import is_email_globally_banned

    if is_email_globally_banned(user.email):
        logger.info("verification_email_resend_blocked_banned_user", user_id=str(user.id), email=email)
        return None

    send_verification_email_for_user(user)
    return None


def request_password_reset(email: str) -> str | None:
    """Request a password reset.

    Args:
        email (str): The email address of the user.

    Returns:
        None
    """
    logger.info("password_reset_requested", email=email)
    try:
        user = RevelUser.objects.get(username=email)
    except RevelUser.DoesNotExist:
        logger.info("password_reset_user_not_found", email=email)
        return None
    if GoogleSSOUser.objects.filter(user=user).exists():
        logger.info("password_reset_blocked_google_sso", user_id=str(user.id), email=email)
        return None
    token = create_password_reset_token(user)
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(tasks.AccountEmail.PASSWORD_RESET, user.email, token=token)
    )
    logger.info("password_reset_email_sent", user_id=str(user.id), email=email)
    return token


def request_account_deletion(user: RevelUser) -> str:
    """Request account deletion.

    Args:
        user (RevelUser): The user.

    Returns:
        str: The token.
    """
    logger.info("account_deletion_requested", user_id=str(user.id), email=user.email)
    token = create_deletion_token(user)
    transaction.on_commit(lambda: tasks.send_account_email.delay(tasks.AccountEmail.DELETION, user.email, token=token))
    return token


@transaction.atomic
def reset_password(token: str, new_password: str) -> RevelUser:
    """Reset a user's password.

    Args:
        token (str): The password reset token.
        new_password (str): The new password.
    """
    payload = token_to_payload(token, schema.PasswordResetJWTPayloadSchema)
    check_blacklist(payload.jti)
    user = get_object_or_404(RevelUser, id=payload.user_id)
    if GoogleSSOUser.objects.filter(user=user).exists():
        logger.warning("password_reset_blocked_google_sso_user", user_id=str(user.id), email=user.email)
        raise HttpError(400, str(_("Cannot reset password for Google SSO users.")))
    validate_password(new_password, user=user)
    user.set_password(new_password)

    # Convert guest user to full user when setting password
    if user.guest:
        user.guest = False
        user.email_verified = True
        user.save(update_fields=["password", "guest", "email_verified"])
        logger.info("guest_user_converted_to_full_user", user_id=str(user.id), email=user.email)
    else:
        user.save(update_fields=["password"])

    blacklist_token(token)
    logger.info("password_reset_completed", user_id=str(user.id), email=user.email)
    return user


def create_email_change_token(user: RevelUser, new_email: str) -> str:
    """Create a single-use email change token bound to the user and the proposed new email.

    Args:
        user: The user requesting the change.
        new_email: The proposed new email (must already be lowercased).

    Returns:
        The signed JWT token.
    """
    payload = schema.EmailChangeJWTPayloadSchema(
        user_id=user.id,
        email=user.email,
        new_email=new_email,
        exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME,
    )
    token = create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)
    store_test_token(TOKEN_TYPE_EMAIL_CHANGE, token)
    return token


def _mask_email(email: str) -> str:
    """Return a partially-masked rendering of an email for the notice-to-old-address.

    Examples:
        ``alice@example.com`` -> ``a****@example.com``
        ``a@example.com``     -> ``*@example.com``
    """
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{domain}"


def request_email_change(user: RevelUser, new_email: str, password: str) -> str:
    """Begin an email change flow: validate, issue a token, dispatch emails.

    Args:
        user: The authenticated user requesting the change.
        new_email: The lowercased new email address.
        password: The user's current password (defense-in-depth).

    Returns:
        The signed email-change token (also dispatched to the new address).
    """
    from accounts.service.global_ban_service import is_email_globally_banned

    logger.info("email_change_requested", user_id=str(user.id), new_email=new_email)

    # SSO check must run before the password check — SSO accounts have a sentinel
    # password and would otherwise be rejected with a misleading "Incorrect password".
    if GoogleSSOUser.objects.filter(user=user).exists():
        logger.info("email_change_blocked_google_sso", user_id=str(user.id))
        raise HttpError(400, str(_("Google SSO users cannot change their email here.")))

    if not user.check_password(password):
        logger.warning("email_change_bad_password", user_id=str(user.id))
        raise HttpError(400, str(_("Incorrect password.")))

    if new_email == user.email:
        raise HttpError(400, str(_("The new email is the same as the current one.")))

    if is_email_globally_banned(new_email):
        # Mirror the verification flow: silently no-op to avoid signalling ban presence.
        logger.info("email_change_blocked_banned_target", user_id=str(user.id))
        return ""

    if RevelUser.objects.filter(username__iexact=new_email).exists():
        raise HttpError(400, str(_("This email is already in use.")))

    token = create_email_change_token(user, new_email)
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(tasks.AccountEmail.CHANGE_CONFIRMATION, new_email, token=token)
    )
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.CHANGE_NOTICE, user.email, context={"masked_new_email": _mask_email(new_email)}
        )
    )
    logger.info("email_change_email_sent", user_id=str(user.id), new_email=new_email)
    return token


def confirm_email_change(token: str) -> RevelUser:
    """Confirm an email change and rotate the user's identity.

    Validates and blacklists the token, swaps ``email``/``username`` to the new
    address, blacklists every outstanding JWT for the user (treating email as
    an identity primitive), and dispatches notifications to both addresses.

    Args:
        token: The email-change JWT.

    Returns:
        The user with the updated email.

    Raises:
        HttpError: 400 if the new address is already taken (including under a
            DB-level race), 403 if the address became globally banned between
            request and confirm.
    """
    from accounts.service.global_ban_service import BAN_ERROR_MESSAGE, is_email_globally_banned

    payload = token_to_payload(token, schema.EmailChangeJWTPayloadSchema)
    check_blacklist(payload.jti)
    user = get_object_or_404(RevelUser, id=payload.user_id)
    new_email = payload.new_email.lower()

    # Re-check the global ban at confirm time — a ban added between request and
    # confirm must block the swap (mirrors verify_email).
    if is_email_globally_banned(new_email):
        blacklist_token(token)
        logger.warning("email_change_confirm_blocked_banned_target", user_id=str(user.id))
        raise HttpError(403, str(BAN_ERROR_MESSAGE))

    if RevelUser.objects.filter(username__iexact=new_email).exclude(pk=user.pk).exists():
        blacklist_token(token)
        logger.warning("email_change_confirm_email_taken", user_id=str(user.id), new_email=new_email)
        raise HttpError(400, str(_("This email is already in use.")))

    old_email = user.email
    try:
        with transaction.atomic():
            blacklist_token(token)
            user.email = new_email
            user.username = new_email
            user.email_verified = True
            user.save(update_fields=["email", "username", "email_verified"])
            # Email is an identity primitive — invalidate every active session.
            blacklist_user_tokens(user)
    except IntegrityError:
        # Another confirmation took the address between the pre-check and the save.
        # Blacklist outside the rolled-back transaction so the token cannot be retried.
        blacklist_token(token)
        logger.warning("email_change_confirm_race_loss", user_id=str(user.id), new_email=new_email)
        raise HttpError(400, str(_("This email is already in use.")))

    _change_completed_context = {"old_email": old_email, "new_email": new_email}
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.CHANGE_COMPLETED_OLD, old_email, context=_change_completed_context
        )
    )
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.CHANGE_COMPLETED_NEW, new_email, context=_change_completed_context
        )
    )
    logger.info(
        "email_change_confirmed",
        user_id=str(user.id),
        old_email=old_email,
        new_email=new_email,
    )
    return user


@transaction.atomic
def confirm_account_deletion(token: str) -> None:
    """Confirm and execute account deletion.

    This function validates the deletion token and enqueues a background task
    to delete the user account. The actual deletion is handled asynchronously
    via Celery to avoid blocking the request for users with many relationships.

    Args:
        token (str): The account deletion token.

    Raises:
        HttpError: If the user owns organizations (requires manual intervention).
    """
    payload = token_to_payload(token, schema.DeleteAccountJWTPayloadSchema)
    check_blacklist(payload.jti)
    user = get_object_or_404(RevelUser, id=payload.user_id)

    # Check if user owns any organizations - this would block deletion
    if user.owned_organizations.exists():
        org_count = user.owned_organizations.count()
        logger.warning(
            "account_deletion_blocked_owns_organizations", user_id=str(user.id), email=user.email, org_count=org_count
        )
        raise HttpError(
            400,
            str(
                _(
                    "You cannot delete your account while you own organizations. "
                    "Please contact support to transfer ownership or delete the organizations first."
                )
            ),
        )

    logger.info("account_deletion_confirmed", user_id=str(user.id), email=user.email)
    blacklist_token(token)
    # Enqueue the deletion as a background task to handle heavy operations
    transaction.on_commit(lambda: tasks.delete_user_account.delay(str(user.id)))


def update_profile(user: RevelUser, payload: schema.ProfileUpdateSchema) -> RevelUser:
    """Partially update a user's profile.

    Applies only the fields explicitly provided in the payload, persists them
    with a targeted ``update_fields`` save, and refreshes from the DB so any
    derived/file fields are reflected accurately.

    Args:
        user: The authenticated user being updated.
        payload: The profile update schema (partial update via ``exclude_unset``).

    Returns:
        The updated user instance.
    """
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        return user
    for key, value in update_data.items():
        setattr(user, key, value)
    user.save(update_fields=list(update_data.keys()))
    # Refresh from DB to ensure all fields (including file fields) have correct values
    user.refresh_from_db()
    return user


def update_language(user: RevelUser, language: str) -> RevelUser:
    """Update a user's preferred language.

    Args:
        user: The authenticated user.
        language: The new language code (already validated by the schema).

    Returns:
        The updated user instance.
    """
    user.language = language
    user.save(update_fields=["language"])
    return user


def start_data_export(user: RevelUser) -> None:
    """Enqueue an asynchronous GDPR-compliant data export for a user.

    Args:
        user: The authenticated user requesting the export.
    """
    # Defer dispatch until the surrounding transaction commits. The data
    # export task reads the user row, which under ATOMIC_REQUESTS=True is
    # not visible to the worker until the request commits.
    transaction.on_commit(lambda: tasks.generate_user_data_export.delay(str(user.pk)))


T = t.TypeVar("T", bound=Schema)


def token_to_payload(token: str, schema_class: t.Type[T]) -> T:
    """Decode a token and validate it against a schema.

    Args:
        token (str): The token to decode.
        schema_class (t.Type[Schema]): The schema to validate the token against.

    Returns:
        Schema: The decoded and validated token.
    """
    try:
        _payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM], audience=settings.JWT_AUDIENCE
        )
        return schema_class.model_validate(_payload)
    except jwt.ExpiredSignatureError:
        logger.warning("token_validation_expired", token_type=schema_class.__name__)
        raise HttpError(400, str(_("Token has expired.")))
    except Exception as e:
        logger.warning("token_validation_failed", token_type=schema_class.__name__, error=str(e))
        raise HttpError(400, str(_("Invalid token: {error}")).format(error=e))

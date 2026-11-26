"""Service layer for the authentication app."""

import typing as t

import pyotp
import structlog
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from accounts import schema, tasks
from accounts.dispatch import guest_converted
from accounts.exceptions import (
    EmailChangeError,
    EmailChangeErrorCode,
    ReferralForfeitureConfirmationRequiredError,
)
from accounts.jwt import blacklist as blacklist_token
from accounts.jwt import blacklist_user_tokens, check_blacklist
from accounts.models import EmailVerificationReminderTracking, Referral, ReferralCode, RevelUser
from accounts.password_validation import validate_password
from accounts.service import referral_application_service
from accounts.service.account_tokens import (
    create_deletion_token as create_deletion_token,
)
from accounts.service.account_tokens import (
    create_email_change_token as create_email_change_token,
)
from accounts.service.account_tokens import (
    create_password_reset_token as create_password_reset_token,
)
from accounts.service.account_tokens import (
    create_verification_token as create_verification_token,
)
from accounts.service.account_tokens import (
    token_to_payload as token_to_payload,
)
from accounts.service.referral_cleanup import assess_referral_forfeiture
from common.thumbnails.service import delete_image_with_derivatives

logger = structlog.get_logger(__name__)


def _email_context_kwargs(
    return_host: str | None,
    *,
    return_url: str | None = None,
    language: str | None = None,
    deletion_token: str | None = None,
) -> dict[str, t.Any]:
    """Task kwargs carrying the optional link hints inside the ``context`` dict.

    ``return_host`` (white-label, #1156) joins ``return_url`` in the same ``context`` dict; it is never
    a task kwarg of its own. A worker that predates #1156 accepts the message, ignores the unknown
    key and sends the email without the hint. Empty without any hint, so those messages keep their
    exact pre-existing shape. ``language`` (the recipient's) rides along the same way so the worker
    renders the email in it. ``deletion_token`` (verification email only) lets the worker add the
    "this wasn't me" account-deletion link; an old worker ignores it, and a message without it
    renders the email without that line.
    """
    context: dict[str, str] = {}
    if language:
        context["language"] = language
    if return_url is not None:
        context["return_url"] = return_url
    if return_host:
        context["return_host"] = return_host
    if deletion_token:
        context["deletion_token"] = deletion_token
    return {"context": context} if context else {}


def _send_activation_email_for_guest(user: RevelUser, *, return_host: str | None = None) -> None:
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
    tasks.send_account_email.delay(
        tasks.AccountEmail.ACTIVATION,
        user.email,
        token=token,
        **_email_context_kwargs(return_host, language=user.language),
    )
    logger.info("account_activation_email_sent", user_id=str(user.id), email=user.email)


def reset_unverified_credentials(user: RevelUser, *, password: str | None) -> None:
    """Drop every credential set before now on an account whose email nobody has verified yet.

    Nothing proved the address belonged to whoever set those credentials, so the account starts
    over: the given password (or an unusable one), no 2FA enrollment, no live sessions, no linked
    Telegram account. ``credentials_changed_at`` voids earlier access tokens, pending email-change
    links and earlier verification links. Does not touch ``email_verified``: callers decide.

    Args:
        user: The unverified account.
        password: The new password, already validated; ``None`` leaves the account without a usable one.
    """
    if password is None:
        user.set_unusable_password()
    else:
        user.set_password(password)
    user.totp_active = False
    user.totp_secret = pyotp.random_base32()
    user.credentials_changed_at = timezone.now()
    user.save(update_fields=["password", "totp_active", "totp_secret", "credentials_changed_at"])
    blacklist_user_tokens(user)
    user.telegram_users.update(user=None)


# The lookup filter of the emailed links (verify, reset): an erased account or one whose deletion is pending
# (#1476) is answered as if the row were gone, so a link never reactivates it or tells that a deletion is pending.
_NOT_BEING_DELETED = {"erased_at__isnull": True, "deletion_requested_at__isnull": True}


def _mark_email_verified(user: RevelUser) -> None:
    """Activate the account, mark its email verified and stop the reminders.

    An approved referral invitee is enrolled by the ``post_save`` receiver watching this transition.
    """
    user.is_active = user.email_verified = True
    user.signup_contested_at = None
    user.save(update_fields=["is_active", "email_verified", "signup_contested_at"])
    # Delete reminder tracking record to stop all reminder emails
    EmailVerificationReminderTracking.objects.filter(user=user).delete()


def _reset_contested_profile(user: RevelUser) -> None:
    """Drop what the account holds that somebody else may have entered: profile and referral (names, language stay)."""
    delete_image_with_derivatives(user, "profile_picture")
    user.preferred_name = user.pronouns = user.bio = ""
    user.phone_number = None
    user.save(update_fields=["preferred_name", "pronouns", "bio", "phone_number"])
    Referral.objects.filter(referred_user=user).delete()


def adopt_unverified_account(user: RevelUser, *, password: str | None) -> None:
    """Adopt an unverified account (OIDC login, a password reset or verifying a contested one): it starts over.

    The first proof of ownership arrived, so the account is verified.

    Credentials set before now are dropped (see :func:`reset_unverified_credentials`). When somebody signed
    up again with the address meanwhile (``signup_contested_at``), the profile and referral are reset too,
    since either signup may have entered them. Callers verify the proof (an emailed link or an IdP assertion).

    Args:
        user: The unverified, non-guest account.
        password: The new password, already validated; ``None`` leaves the account without a usable one.
    """
    reset_unverified_credentials(user, password=password)
    if user.signup_contested_at is not None:
        _reset_contested_profile(user)
    _mark_email_verified(user)


def _apply_referral_code(user: RevelUser, referral_code_obj: ReferralCode | None) -> None:
    """Record that ``user`` signed up with ``referral_code_obj`` (no-op without a code)."""
    if referral_code_obj is None:
        return
    Referral.objects.create(referral_code=referral_code_obj, referred_user=user)
    logger.info(
        "referral_created",
        user_id=str(user.id),
        referrer_id=str(referral_code_obj.user_id),
        code=referral_code_obj.code,
    )


def _contest_unverified_signup(
    user: RevelUser, payload: schema.RegisterUserSchema, *, return_host: str | None
) -> tuple[RevelUser, str]:
    """Repeat signup on an unverified address: note it, send a fresh link and change nothing else.

    Nobody proved who owns the address, so the stored account (password, profile, referral, 2FA,
    sessions) is left alone; verifying it later restarts it (see :func:`adopt_unverified_account`).
    The returned user is built from the submitted payload only, so the answer reveals nothing stored.
    An account with a pending or finished deletion gets neither the mark nor the link; the answer is the same.
    """
    logger.info("user_registration_contested_unverified", user_id=str(user.id), email=payload.email)
    if user.erased_at is None and user.deletion_requested_at is None:
        user.signup_contested_at = timezone.now()
        user.save(update_fields=["signup_contested_at"])
        _, token = send_verification_email_for_user(user, return_url=payload.return_url, return_host=return_host)
    else:
        # Deletion is pending: no link (``verify_email`` would refuse it) and no mark, same answer.
        token = create_verification_token(user)
    echo = RevelUser(
        username=payload.email, email=payload.email, first_name=payload.first_name, last_name=payload.last_name
    )
    return echo, token


@transaction.atomic
def register_user(payload: schema.RegisterUserSchema, *, return_host: str | None = None) -> tuple[RevelUser, str]:
    """Register a new user, and send a verification email.

    If the email belongs to an existing guest user, sends an account activation
    email (password-reset link) instead of converting the account directly.
    This prevents account takeover — only the real email owner can complete
    activation.

    If the email belongs to an unverified account, that account is only marked as contested and
    sent a fresh verification link (see :func:`_contest_unverified_signup`); a repeat signup never
    changes what the account stores, and the referral code is validated but not applied.

    Uses ``select_for_update`` on the existing-user lookup so that concurrent
    registrations for the same email serialise correctly.

    Args:
        payload: The registration data.
        return_host: Optional tenant hostname hint carried in the email link (see
            :func:`accounts.tasks.email.send_account_email`).

    Returns:
        A tuple of the user and the verification token. For a repeat signup on an unverified address
        the user is an unsaved object built from the payload, never the stored account.
    """
    from accounts.service.global_ban_service import BAN_ERROR_MESSAGE, is_email_globally_banned

    logger.info("user_registration_started", email=payload.email)
    if is_email_globally_banned(payload.email):
        raise HttpError(403, str(BAN_ERROR_MESSAGE))
    existing_user = RevelUser.objects.select_for_update().filter(username=payload.email).first()
    if existing_user and (existing_user.guest or existing_user.email_verified):
        # A guest whose deletion is pending or done gets no link (``reset_password`` would refuse it); same answer.
        if existing_user.guest and existing_user.erased_at is None and existing_user.deletion_requested_at is None:
            _send_activation_email_for_guest(existing_user, return_host=return_host)
        logger.warning("user_registration_duplicate", email=payload.email)
        raise HttpError(400, str(_("A user with this email already exists.")))

    # Validate referral code after existing-user check but before creating or changing the user
    referral_code_obj: ReferralCode | None = None
    if payload.referral_code is not None:
        referral_code_obj = ReferralCode.objects.filter(code__iexact=payload.referral_code, is_active=True).first()
        if not referral_code_obj:
            raise HttpError(422, str(_("Invalid or inactive referral code.")))

    if existing_user:
        return _contest_unverified_signup(existing_user, payload, return_host=return_host)

    new_user = RevelUser.objects.create_user(
        username=payload.email,
        email=payload.email,
        password=payload.password1,
        first_name=payload.first_name,
        last_name=payload.last_name,
        is_active=True,  # we use email verification
    )

    _apply_referral_code(new_user, referral_code_obj)

    logger.info("user_registration_completed", user_id=str(new_user.id), email=new_user.email)
    return send_verification_email_for_user(new_user, return_url=payload.return_url, return_host=return_host)


def send_verification_email_for_user(
    user: RevelUser, *, return_url: str | None = None, return_host: str | None = None
) -> tuple[RevelUser, str]:
    """Send a verification email for a user.

    Args:
        user: The user to email. The email goes out after the surrounding transaction commits, so a new user row is
            visible to the worker.
        return_url: Optional relative path (validated by ``RegisterUserSchema``)
            carried in the verification link as ``returnUrl``.
        return_host: Optional tenant hostname hint carried in the link as ``return_host``.
    """
    logger.info("verification_email_requested", user_id=str(user.id), email=user.email)
    token = create_verification_token(user)
    deletion_token = create_deletion_token(user)

    def dispatch() -> None:
        # return_url and return_host both ride in the task's existing ``context`` kwarg, never as
        # new task kwargs: a not-yet-upgraded worker during a rolling deploy accepts the message and
        # drops the keys it does not know, so the email still goes out (without the hint).
        tasks.send_account_email.delay(
            tasks.AccountEmail.VERIFICATION,
            user.email,
            token=token,
            **_email_context_kwargs(
                return_host, return_url=return_url, language=user.language, deletion_token=deletion_token
            ),
        )

    transaction.on_commit(dispatch)
    return user, token


@transaction.atomic
def verify_email(token: str, *, return_host: str | None = None) -> RevelUser:
    """Verify a user's email.

    Automatically reactivates deactivated accounts upon verification and clears
    any reminder tracking to prevent further emails. An erased account, or one whose
    deletion is pending, gets the answer for a missing account. A link minted before the account's last
    credential reset is refused. If somebody signed up again with the address while it was
    unverified, verifying restarts the account (:func:`adopt_unverified_account`) and a
    password-reset email is sent so the person can set a password.

    Args:
        token (str): The verification token.
        return_host: Optional tenant hostname hint carried in the set-password email sent for a
            contested account.

    Returns:
        RevelUser: The verified user.
    """
    from accounts.service.global_ban_service import BAN_ERROR_MESSAGE, is_email_globally_banned

    payload = token_to_payload(token, schema.VerifyEmailJWTPayloadSchema)
    check_blacklist(payload.jti)
    # An erased account, or one whose deletion is pending (#1476), counts as gone, as with a deleted row:
    # verifying would reactivate it.
    if user := RevelUser.objects.select_for_update().filter(id=payload.user_id, **_NOT_BEING_DELETED).first():
        # Guest users must not be verified through this flow — they were never registered
        if user.guest:
            logger.warning("email_verification_blocked_guest_user", user_id=str(user.id), email=user.email)
            raise HttpError(400, str(_("Invalid verification token.")))
        if is_email_globally_banned(user.email):
            blacklist_token(token)
            raise HttpError(403, str(BAN_ERROR_MESSAGE))
        # A link sent before the last credential reset (e.g. before a repeat signup) is void. Whole-second
        # comparison like ``common.auth_base``: ``iat`` is floored, so a link minted just after the reset passes.
        changed_at = user.credentials_changed_at
        if changed_at is not None and (
            payload.iat is None or int(payload.iat.timestamp()) < int(changed_at.timestamp())
        ):
            logger.warning("email_verification_blocked_superseded_token", user_id=str(user.id))
            raise HttpError(400, str(_("Invalid verification token.")))
        blacklist_token(token)
        was_inactive = not user.is_active
        if user.signup_contested_at is not None and not user.email_verified:
            adopt_unverified_account(user, password=None)
            request_password_reset(user.email, return_host=return_host)
        else:
            _mark_email_verified(user)

        logger.info("email_verified", user_id=str(user.id), email=user.email, was_reactivated=was_inactive)
        return user
    logger.warning("email_verification_failed_user_not_found", user_id=str(payload.user_id))
    raise HttpError(400, str(_("A user with this email no longer exists.")))


def resend_verification_email(email: str, *, return_url: str | None = None, return_host: str | None = None) -> None:
    """Resend verification email for a user.

    Silently handles all cases to prevent user enumeration:
    - If user doesn't exist: do nothing
    - If email already verified: do nothing
    - If user exists and unverified: send verification email

    Args:
        email (str): The email address of the user.
        return_url: Optional relative path (validated by ``ResendVerificationSchema``)
            carried in the verification link as ``returnUrl``.
        return_host: Optional tenant hostname hint carried in the link as ``return_host``.
    """
    logger.info("verification_email_resend_requested", email=email)
    try:
        user = RevelUser.objects.get(username=email, **_NOT_BEING_DELETED)  # its link would be refused
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

    send_verification_email_for_user(user, return_url=return_url, return_host=return_host)
    return None


def request_password_reset(email: str, *, return_host: str | None = None) -> str | None:
    """Request a password reset.

    Args:
        email (str): The email address of the user.
        return_host: Optional tenant hostname hint carried in the email link as ``return_host``.

    Returns:
        None
    """
    logger.info("password_reset_requested", email=email)
    try:
        user = RevelUser.objects.get(username=email, **_NOT_BEING_DELETED)  # its link would be refused
    except RevelUser.DoesNotExist:
        logger.info("password_reset_user_not_found", email=email)
        return None

    from accounts.service.global_ban_service import is_email_globally_banned

    # Silently no-op for a banned address (mirrors resend_verification_email) so a
    # banned guest cannot use the reset link to convert into a full account.
    if is_email_globally_banned(user.email):
        logger.info("password_reset_blocked_banned_user", user_id=str(user.id), email=email)
        return None
    token = create_password_reset_token(user)
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.PASSWORD_RESET,
            user.email,
            token=token,
            **_email_context_kwargs(return_host, language=user.language),
        )
    )
    logger.info("password_reset_email_sent", user_id=str(user.id), email=email)
    return token


def request_account_deletion(user: RevelUser, *, return_host: str | None = None) -> str:
    """Request account deletion.

    Args:
        user (RevelUser): The user.
        return_host: Optional tenant hostname hint carried in the email link as ``return_host``.

    Returns:
        str: The token.
    """
    logger.info("account_deletion_requested", user_id=str(user.id), email=user.email)
    token = create_deletion_token(user)
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.DELETION,
            user.email,
            token=token,
            **_email_context_kwargs(return_host, language=user.language),
        )
    )
    return token


def reset_password(token: str, new_password: str) -> RevelUser:
    """Reset a user's password.

    Args:
        token (str): The password reset token.
        new_password (str): The new password.
    """
    from accounts.service.global_ban_service import BAN_ERROR_MESSAGE, is_email_globally_banned

    payload = token_to_payload(token, schema.PasswordResetJWTPayloadSchema)
    check_blacklist(payload.jti)
    # An erased account, or one whose deletion is pending (#1476), is a deleted account: a link sent before
    # the deletion finds nothing, as when the row is gone. Otherwise the reset could verify and reactivate it.
    user = get_object_or_404(RevelUser, id=payload.user_id, **_NOT_BEING_DELETED)
    # Re-check the global ban at reset time — a ban added after the token was issued
    # must block the reset (and the guest-to-full-user conversion below), mirroring
    # verify_email. Blacklist the token so it cannot be retried. This runs OUTSIDE the
    # atomic block below on purpose: raising inside it would roll the blacklist row back
    # along with the savepoint, leaving the token redeemable once the ban is lifted.
    if is_email_globally_banned(user.email):
        blacklist_token(token)
        logger.warning("password_reset_blocked_banned_user", user_id=str(user.id), email=user.email)
        raise HttpError(403, str(BAN_ERROR_MESSAGE))
    return _apply_password_reset(user, token, new_password)


@transaction.atomic
def convert_guest(user: RevelUser, *, reactivate: bool = False) -> bool:
    """Convert a guest into a full account: the single place every conversion path goes through.

    In one transaction: locks the row (a concurrent or repeated conversion sees a non-guest, copies
    the current values onto ``user`` and returns), clears ``guest``, marks the email verified,
    optionally reactivates, enrolls an approved referral invitee and sends ``guest_converted`` (the
    notifications app resets the preferences to the regular defaults, #1248). Nothing is swallowed
    except the referral enrollment, which by design never blocks (see ``try_enroll_invitee``): a
    receiver failure propagates and rolls the conversion back.

    # ponytail: bulk writes (``.update(guest=False)``, shell fixes) bypass this by design; always
    # convert through this service.

    A non-guest is a no-op (``reactivate`` included). This does not check global bans: callers do
    (password reset and OIDC before reaching here, the admin action per user).

    Marking the email verified and enrolling referrals presumes the person owns the email: callers
    (and staff running the admin action) must only convert when that ownership is established.

    Args:
        user: The user to convert.
        reactivate: Also set ``is_active`` (OIDC login revives an inactive guest).

    Returns:
        True only when this call actually converted the user.
    """
    current = (
        RevelUser.objects.select_for_update()
        .values("guest", "email_verified", "is_active", "deletion_requested_at")
        .get(pk=user.pk)
    )
    # A guest whose deletion is pending (#1476) is not converted by a reactivating login: it stays inactive.
    if not current["guest"] or (reactivate and current["deletion_requested_at"] is not None):
        user.guest, user.email_verified, user.is_active = (
            current["guest"],
            current["email_verified"],
            current["is_active"],
        )
        return False
    user.guest = False
    user.email_verified = True
    update_fields = ["guest", "email_verified"]
    if reactivate:
        user.is_active = True
        update_fields.append("is_active")
    user.save(update_fields=update_fields)
    logger.info("guest_user_converted_to_full_user", user_id=str(user.id), email=user.email)
    referral_application_service.try_enroll_invitee(user)
    guest_converted.send(sender=RevelUser, user=user)
    return True


@transaction.atomic
def _apply_password_reset(user: RevelUser, token: str, new_password: str) -> RevelUser:
    """Set the new password and consume the token (the atomic half of ``reset_password``)."""
    # Lock and re-read, like ``verify_email``: a contest, verification or deletion committed concurrently is seen.
    user = get_object_or_404(RevelUser.objects.select_for_update(), pk=user.pk, **_NOT_BEING_DELETED)
    validate_password(new_password, user=user)
    if not user.guest and not user.email_verified:
        # The reset link proves ownership of the address: the account starts over with the new password.
        adopt_unverified_account(user, password=new_password)
    else:
        user.set_password(new_password)
        user.credentials_changed_at = timezone.now()
        user.save(update_fields=["password", "credentials_changed_at"])
        # Setting a password converts a guest to a full user
        if user.guest:
            convert_guest(user)

    blacklist_token(token)
    # Reset is the account-recovery path — sign out everywhere, apps included (#1020).
    blacklist_user_tokens(user)
    logger.info("password_reset_completed", user_id=str(user.id), email=user.email)
    return user


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


def request_email_change(user: RevelUser, new_email: str, password: str, *, return_host: str | None = None) -> str:
    """Begin an email change flow: validate, issue a token, dispatch emails.

    Args:
        user: The authenticated user requesting the change.
        new_email: The lowercased new email address.
        password: The user's current password (defense-in-depth).
        return_host: Optional tenant hostname hint carried in the confirmation link as ``return_host``.

    Returns:
        The signed email-change token (also dispatched to the new address).
    """
    from accounts.service.global_ban_service import is_email_globally_banned

    logger.info("email_change_requested", user_id=str(user.id), new_email=new_email)

    # Identity-only accounts (OIDC-created, or legacy SSO users with password == "") have no
    # password to check; tell them to set one instead of a misleading "Incorrect password".
    if not (user.password and user.has_usable_password()):
        logger.info("email_change_no_password", user_id=str(user.id))
        raise HttpError(400, str(_("Set a password before changing your email.")))
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
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.CHANGE_CONFIRMATION,
            new_email,
            token=token,
            **_email_context_kwargs(return_host, language=user.language),
        )
    )
    transaction.on_commit(
        lambda: tasks.send_account_email.delay(
            tasks.AccountEmail.CHANGE_NOTICE,
            user.email,
            context={"masked_new_email": _mask_email(new_email), "language": user.language},
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
        EmailChangeError: 400 if the new address is already taken (including under a
            DB-level race) or the link was superseded by a password reset.
        HttpError: 403 if the address became globally banned between request and confirm.
    """
    from accounts.service.global_ban_service import BAN_ERROR_MESSAGE, is_email_globally_banned

    payload = token_to_payload(token, schema.EmailChangeJWTPayloadSchema)
    check_blacklist(payload.jti)
    user = get_object_or_404(RevelUser, id=payload.user_id)
    new_email = payload.new_email.lower()

    # The link goes to the *new* address, so whoever requested it holds it. A password reset
    # (account recovery) must void it, or an evicted intruder confirms it and takes the account.
    if user.credentials_changed_at and payload.iat < user.credentials_changed_at:
        blacklist_token(token)
        logger.warning("email_change_confirm_superseded_by_password_reset", user_id=str(user.id))
        raise EmailChangeError(
            EmailChangeErrorCode.EMAIL_CHANGE_SUPERSEDED,
            str(_("This email change link is no longer valid. Please request a new one.")),
        )

    # Re-check the global ban at confirm time — a ban added between request and
    # confirm must block the swap (mirrors verify_email).
    if is_email_globally_banned(new_email):
        blacklist_token(token)
        logger.warning("email_change_confirm_blocked_banned_target", user_id=str(user.id))
        raise HttpError(403, str(BAN_ERROR_MESSAGE))

    if RevelUser.objects.filter(username__iexact=new_email).exclude(pk=user.pk).exists():
        blacklist_token(token)
        logger.warning("email_change_confirm_email_taken", user_id=str(user.id), new_email=new_email)
        raise EmailChangeError(EmailChangeErrorCode.EMAIL_TAKEN, str(_("This email is already in use.")))

    old_email = user.email
    try:
        with transaction.atomic():
            # Re-check under the row lock: a credential reset that committed after the unlocked
            # check above (it holds this lock while it runs) must still void the link.
            changed_at = (
                RevelUser.objects.select_for_update().values_list("credentials_changed_at", flat=True).get(pk=user.pk)
            )
            superseded = bool(changed_at and payload.iat < changed_at)
            if not superseded:
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
        raise EmailChangeError(EmailChangeErrorCode.EMAIL_TAKEN, str(_("This email is already in use.")))
    if superseded:
        blacklist_token(token)
        logger.warning("email_change_confirm_superseded_by_password_reset", user_id=str(user.id))
        raise EmailChangeError(
            EmailChangeErrorCode.EMAIL_CHANGE_SUPERSEDED,
            str(_("This email change link is no longer valid. Please request a new one.")),
        )

    _change_completed_context = {"old_email": old_email, "new_email": new_email, "language": user.language}
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
def confirm_account_deletion(token: str, force: bool = False) -> None:
    """Confirm and execute account deletion.

    This function validates the deletion token and enqueues a background task
    to delete the user account. The actual deletion is handled asynchronously
    via Celery to avoid blocking the request for users with many relationships.

    Args:
        token (str): The account deletion token.
        force (bool): Acknowledge the reported consequences (unpaid referral
            payouts) and proceed anyway.

    Raises:
        HttpError: If the user owns organizations (requires manual intervention).
        ReferralForfeitureConfirmationRequiredError: If deleting would forfeit
            unpaid referral earnings and ``force`` was not set. The token is not
            burned, so the user can re-submit it with ``force=True``.
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

    # Referral forfeiture needs explicit acknowledgement. UX only — the deletion
    # task re-derives the state and is authoritative. Runs before blacklist_token
    # so the 409 leaves the token usable for the forced re-submission.
    if not force:
        forfeiture = assess_referral_forfeiture(user)
        if forfeiture is not None:
            logger.info(
                "account_deletion_requires_referral_confirmation",
                user_id=str(user.id),
                unpaid_count=forfeiture.unpaid_count,
                unpaid_total=str(forfeiture.unpaid_total),
            )
            raise ReferralForfeitureConfirmationRequiredError(forfeiture)

    logger.info("account_deletion_confirmed", user_id=str(user.id), email=user.email, force=force)
    blacklist_token(token)
    # Sign-in stops now, in this transaction, not only when the background task has run. Only an account
    # that was active gets the mark: a failed deletion must not give access back to an account that an
    # admin or the unverified sweep deactivated.
    mark = timezone.now()
    marked = RevelUser.objects.filter(pk=user.pk, is_active=True).update(is_active=False, deletion_requested_at=mark)
    user_id = str(user.id)
    # The run carries its mark: only the run of this mark can give the access back when it fails for good.
    deletion_mark = mark.isoformat() if marked else None
    # One run for each mark. A second confirmation while the first run is queued or in flight queues nothing:
    # the two runs would share the mark, and a failure of one would give the access back while the other
    # still deletes the account. The UPDATE above waits for a concurrent confirmation, so this read sees its mark.
    if not marked and RevelUser.objects.filter(pk=user.pk, deletion_requested_at__isnull=False).exists():
        logger.info("account_deletion_already_pending", user_id=user_id)
        return

    # A named closure, not a lambda or partial: robust on_commit needs a name to log.
    def dispatch_account_deletion() -> None:
        try:
            tasks.delete_user_account.delay(user_id, deletion_mark=deletion_mark)
        except Exception:
            # The request has already answered that the deletion will run: the account stays inactive with its
            # mark, and ``requeue_stale_account_deletions`` queues the run again.
            logger.error("account_deletion_dispatch_failed", user_id=user_id, exc_info=True)

    transaction.on_commit(dispatch_account_deletion, robust=True)


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

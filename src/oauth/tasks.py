"""Beat sweeps for the OAuth provider (spec §10).

Both run daily and unconditionally — they are garbage collection over DOT's tables, so they
must keep working after the provider is switched off (ADR-0008) rather than leaving whatever
was issued while it was on to sit in the database for ever.
"""

import datetime as dt

import structlog
from celery import shared_task
from django.conf import settings
from django.core.management import call_command
from django.db.models import Exists, OuterRef
from django.utils import timezone
from oauth2_provider.models import AccessToken, Grant
from oauth2_provider.settings import oauth2_settings

from oauth.models import OAuthApplication

logger = structlog.get_logger(__name__)


@shared_task(name="oauth.clear_expired_tokens")
def clear_expired_tokens() -> None:
    """Delete expired access/refresh/ID tokens and grants (DOT's ``cleartokens``).

    Thin on purpose: DOT owns the definition of "expired" here, including the
    ``REFRESH_TOKEN_REUSE_PROTECTION`` tombstones it must keep until the refresh token they
    protect ages out. Re-deriving that would be a second, divergent answer to the same question.
    """
    call_command("cleartokens")


@shared_task(name="oauth.prune_unused_dynamic_clients")
def prune_unused_dynamic_clients() -> int:
    """Delete dynamically registered apps that never completed an authorization within the TTL.

    RFC 7591 registration is unauthenticated, so the table is a spam surface: an abandoned
    client row is indistinguishable from a probe. But "unused" is a fact about the app's
    *history*, not about what it happens to hold right now (R-114): a user who authorizes a
    dynamic client and then disconnects it, or a grant whose only access token ``cleartokens``
    has since reaped, leaves an app with no artifact at all. Deleting it there would break the
    ``client_id`` for every *other* user of that client, and an MCP host that cached its
    registration would get ``invalid_client`` instead of re-registering. So the primary signal
    is ``last_used_at``, which Task 4 writes only on genuine app-token API use (a
    registration-scope token is refused before the bump) and which nothing ever clears.

    The two artifact checks stay as belt and braces for a client that registered and authorized
    inside the TTL without an API call yet. A grant only protects a code that is mid-flight or
    expired-unredeemed — DOT deletes the grant row at code exchange
    (``oauth2_validators.py:493``), so a redeemed one is already gone and it is ``last_used_at``
    that carries the history.

    The registration credential DOT mints for every dynamic client (its scope is exactly
    ``DCR_REGISTRATION_SCOPE``, ``oauth2_provider/views/dynamic_client_registration.py:239``) is
    not use: it is issued by the registration call itself, so counting it would mean nothing is
    ever pruned. The match is exact rather than ``__contains`` — unlike ``connections_for``,
    which excludes registration tokens from a *listing* and can afford to over-exclude, an
    over-broad exclusion here would classify a real token as a registration credential and
    delete a live client. Exact matching errs towards keeping an app, which is the only safe
    direction for a destructive sweep.

    Returns:
        The number of *applications* deleted. Read out of ``delete()``'s per-model dict rather
        than its first element, which is the total cascade row count — apps plus their tokens,
        grants and ID tokens — and would over-report by a factor of however many credentials
        each app happened to hold (R-23).
    """
    cutoff = timezone.now() - dt.timedelta(hours=settings.OAUTH_DCR_UNUSED_TTL_HOURS)
    real_tokens = AccessToken.objects.filter(application=OuterRef("pk")).exclude(
        scope=oauth2_settings.DCR_REGISTRATION_SCOPE
    )
    grants = Grant.objects.filter(application=OuterRef("pk"))
    _total, per_model = (
        OAuthApplication.objects.filter(
            ~Exists(real_tokens),
            ~Exists(grants),
            registration_source=OAuthApplication.RegistrationSource.DCR,
            last_used_at__isnull=True,
            created__lt=cutoff,
        )
    ).delete()
    deleted = per_model.get(OAuthApplication._meta.label, 0)
    logger.info("oauth_dynamic_clients_pruned", count=deleted)
    return deleted

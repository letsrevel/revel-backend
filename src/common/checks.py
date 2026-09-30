"""Deploy-time validation of the instance's public URLs and sender domain (#1039).

Registered from ``CommonConfig.ready()``. Everything is skipped under ``DEBUG``. A local or
empty ``BASE_URL`` is an Error, so a production instance fails ``manage.py check`` (and
therefore ``migrate`` in the web entrypoint) instead of emailing links — including
``List-Unsubscribe`` one-click URLs — that point at a host which isn't this API.
The rest are Warnings: they degrade links or deliverability but don't strand users.
No DB access, so the checks run before migrations.
"""

import typing as t
from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import CheckMessage, Error, Warning, register

from common.utils import apex_email_domain

BASE_URL_LOCAL_CHECK_ID = "common.E001"
BASE_URL_HTTPS_CHECK_ID = "common.W002"
FRONTEND_BASE_URL_LOCAL_CHECK_ID = "common.W003"
FOREIGN_SENDER_DOMAIN_CHECK_ID = "common.W004"

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_PROJECT_DOMAIN = "letsrevel.io"


def _host(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower()


def _on_project_domain(host: str) -> bool:
    return host == _PROJECT_DOMAIN or host.endswith(f".{_PROJECT_DOMAIN}")


@register()
def check_deploy_urls(app_configs: t.Any, **kwargs: t.Any) -> list[CheckMessage]:
    """Production instances must advertise their own public origins.

    Args:
        app_configs: Django's app filter (unused; the check is global).
        **kwargs: Django's check kwargs (unused).

    Returns:
        The problems found; nothing while ``DEBUG`` is on.
    """
    if settings.DEBUG:
        return []
    messages: list[CheckMessage] = []
    base_url = str(settings.BASE_URL or "")
    base_host = _host(base_url)

    if not base_host or base_host in _LOCAL_HOSTS:
        messages.append(
            Error(
                f"BASE_URL is {base_url!r}, which is not a public address, while DEBUG is off.",
                hint=(
                    "Set BASE_URL to this API's public HTTPS origin (e.g. https://api.example.org). "
                    "It builds absolute backend links in emails, including List-Unsubscribe."
                ),
                id=BASE_URL_LOCAL_CHECK_ID,
            )
        )
    elif urlsplit(base_url.strip()).scheme != "https":
        messages.append(
            Warning(
                f"BASE_URL {base_url!r} does not use https.",
                hint="Mail providers only honour HTTPS one-click unsubscribe links; use an https:// origin.",
                id=BASE_URL_HTTPS_CHECK_ID,
            )
        )

    if _host(str(settings.FRONTEND_BASE_URL or "")) in _LOCAL_HOSTS:
        messages.append(
            Warning(
                f"FRONTEND_BASE_URL is {settings.FRONTEND_BASE_URL!r} while DEBUG is off.",
                hint=(
                    "Set FRONTEND_BASE_URL to the web app's public origin. Also check "
                    "SiteSettings.frontend_base_url in the admin: it is seeded once from this setting "
                    "and email links use it."
                ),
                id=FRONTEND_BASE_URL_LOCAL_CHECK_ID,
            )
        )

    if (
        base_host not in _LOCAL_HOSTS | {""}
        and _on_project_domain(apex_email_domain())
        and not _on_project_domain(base_host)
    ):
        messages.append(
            Warning(
                f"DEFAULT_FROM_EMAIL sends as {_PROJECT_DOMAIN}, but this instance runs at {base_host}.",
                hint=(
                    "Set DEFAULT_FROM_EMAIL to an address on a domain you control and have authenticated "
                    "(SPF/DKIM) with your email provider."
                ),
                id=FOREIGN_SENDER_DOMAIN_CHECK_ID,
            )
        )
    return messages

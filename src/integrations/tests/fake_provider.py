"""In-memory ``ListingProvider`` used by every non-translator test."""

import typing as t
from urllib.parse import urlencode

from django.http import HttpRequest

from integrations.exceptions import ProviderError
from integrations.providers.base import Capabilities, RemoteAccount, TokenSet, WebhookNotification
from integrations.schema import IntegrationErrorCode


class FakeProvider:
    key: t.ClassVar[str] = "fake"
    display_name: t.ClassVar[str] = "Fake"
    capabilities: t.ClassVar[Capabilities] = Capabilities(
        requires_end_time=True,
        requires_capacity=True,
        supports_structured_content=False,
        supports_unpublish_with_orders=True,
        single_currency_per_event=True,
    )

    def __init__(self, accounts: list[RemoteAccount] | None = None) -> None:
        self.accounts = accounts or [RemoteAccount(remote_id="acc-1", name="Fake Org")]
        self.exchanged: list[str] = []
        self.revoked: list[str] = []
        self.webhooks: dict[str, str] = {}  # remote webhook id -> url
        self.fail_exchange: ProviderError | None = None
        self.fail_webhook: ProviderError | None = None
        self._counter = 0

    def authorize_url(self, state: str, redirect_uri: str) -> str:
        return "https://fake.example/authorize?" + urlencode({"state": state, "redirect_uri": redirect_uri})

    def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        if self.fail_exchange:
            raise self.fail_exchange
        self.exchanged.append(code)
        return TokenSet(access_token=f"tok-{code}")

    def revoke(self, token: TokenSet) -> None:
        self.revoked.append(token.access_token)

    def list_accounts(self, token: TokenSet) -> list[RemoteAccount]:
        if token.access_token == "tok-revoked":
            raise ProviderError(IntegrationErrorCode.CONNECTION_REVOKED, "401")
        return list(self.accounts)

    def register_webhook(self, token: TokenSet, account_id: str, url: str) -> str:
        if self.fail_webhook:
            raise self.fail_webhook
        self._counter += 1
        wid = f"wh-{self._counter}"
        self.webhooks[wid] = url
        return wid

    def unregister_webhook(self, token: TokenSet, remote_webhook_id: str) -> None:
        self.webhooks.pop(remote_webhook_id, None)

    def parse_webhook(self, request: HttpRequest) -> WebhookNotification:
        import orjson

        body: dict[str, t.Any] = orjson.loads(request.body or b"{}")
        if "action" not in body or "path" not in body:
            raise ProviderError(IntegrationErrorCode.PROVIDER_REJECTED, "malformed")
        return WebhookNotification(action=body["action"], resource_path=body["path"], raw=body)

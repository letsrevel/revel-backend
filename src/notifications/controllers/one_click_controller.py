"""RFC 8058 one-click unsubscribe endpoint, targeted by the List-Unsubscribe header (#1029)."""

from urllib.parse import quote

from django.http import HttpResponseRedirect
from django.utils.translation import gettext as _
from ninja_extra import ControllerBase, api_controller, route

from common.models import SiteSettings
from common.schema import ResponseMessage
from notifications.service.unsubscribe import one_click_unsubscribe


# No auth and no throttle: mailbox providers POST from shared IP pools, in bursts.
@api_controller("/notification-preferences", tags=["Notification Preferences"], auth=None)
class OneClickUnsubscribeController(ControllerBase):
    """One-click unsubscribe for mailbox providers (POST) and humans clicking the header link (GET)."""

    @route.post("/one-click", response=ResponseMessage, url_name="one_click_unsubscribe")
    def one_click(self, token: str) -> ResponseMessage:
        """Apply a one-click unsubscribe (RFC 8058).

        The token travels in the query string; the ``List-Unsubscribe=One-Click`` form
        body is ignored. The effect depends on the token: mute the organization (org
        announcements), stop email for that notification type, stop email altogether
        (digest), or opt an address out of invitation emails. Idempotent.
        """
        one_click_unsubscribe(token)
        return ResponseMessage(message=str(_("You have been unsubscribed.")))

    @route.get("/one-click", response={302: None}, url_name="one_click_unsubscribe_redirect")
    def one_click_redirect(self, token: str) -> HttpResponseRedirect:
        """Redirect a browser to the frontend unsubscribe page. Never mutates anything."""
        frontend_base_url = SiteSettings.get_solo().frontend_base_url
        return HttpResponseRedirect(f"{frontend_base_url}/unsubscribe?token={quote(token)}")

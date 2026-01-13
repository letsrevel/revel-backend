from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja import Query
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate
from ninja_extra.searching import Searching, searching

from common.authentication import I18nJWTAuth
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import filters, models, schema
from events.controllers.permissions import EventPermission
from events.service import event_service, update_db_instance

from .base import EventAdminBaseController


@api_controller(
    "/event-admin/{event_id}",
    auth=I18nJWTAuth(),
    permissions=[EventPermission("invite_to_event")],
    tags=["Event Admin"],
    throttle=WriteThrottle(),
)
class EventAdminTokensController(EventAdminBaseController):
    """Event token management endpoints."""

    @route.put(
        "/tokens/{token_id}",
        url_name="edit_event_token",
        response=schema.EventTokenSchema,
    )
    def update_event_token(
        self, event_id: UUID, token_id: str, payload: schema.EventTokenUpdateSchema
    ) -> models.EventToken:
        """Update an existing event token's configuration.

        Event tokens are shareable codes/links that allow users to claim invitations to your event.
        Use this endpoint to modify token settings after creation.

        **Use Cases:**
        - Change the expiration date to extend or shorten token validity
        - Update the maximum number of uses (e.g., limit invites to 50 people)
        - Change which ticket tier users get when claiming (e.g., switch from VIP to General)
        - Update the token name for better organization
        - Modify custom invitation metadata (welcome message, special instructions, etc.)

        **Parameters:**
        - `name`: Optional display name to help you identify this token (e.g., "Alumni Link", "Early Bird")
        - `max_uses`: Maximum number of times this token can be claimed (0 = unlimited)
        - `expires_at`: When the token becomes invalid (users can't claim after this time)
        - `ticket_tier_id`: Which ticket tier to assign when users claim (required for ticketed events)
        - `invitation`: Custom invitation metadata like welcome messages, special flags, etc.

        **Business Logic:**
        - The token's usage count (how many times it's been claimed) is NOT reset when updating
        - If you set max_uses lower than current uses, the token becomes inactive
        - Changing the tier only affects future claims, not existing invitations
        - The token ID itself never changes

        **Frontend Implementation:**
        Display a token management UI where organizers can:
        1. View token stats (uses, expiration, link)
        2. Edit token settings with a form
        3. Show validation errors if tier_id doesn't match event
        4. Warn when reducing max_uses below current usage
        """
        event = self.get_one(event_id)
        if payload.ticket_tier_id:
            get_object_or_404(models.TicketTier, pk=payload.ticket_tier_id, event=event)
        token = get_object_or_404(models.EventToken, pk=token_id)
        return update_db_instance(token, payload)

    @route.delete(
        "/tokens/{token_id}",
        url_name="delete_event_token",
        response={204: None},
    )
    def delete_event_token(self, event_id: UUID, token_id: str) -> tuple[int, None]:
        """Permanently delete an event token and invalidate all links using it.

        **Use Cases:**
        - Revoke access when a shareable link is compromised or leaked publicly
        - Clean up expired or unused tokens
        - Remove tokens after an event closes or capacity is reached
        - Invalidate outdated promotional links

        **Important Warnings:**
        - This action is IRREVERSIBLE - the token and its link become permanently invalid
        - Users with the link will no longer be able to claim invitations
        - However, users who ALREADY claimed invitations keep their access (their EventInvitations persist)
        - The token's usage statistics are lost

        **Frontend Implementation:**
        1. Show a confirmation dialog: "Delete this token? The link will stop working immediately."
        2. Clarify that existing invitations from this token remain valid
        3. Remove the token from the list immediately after successful deletion
        4. Consider showing a "copy shareable link" button before deletion for archival
        """
        event = self.get_one(event_id)
        token = get_object_or_404(models.EventToken, pk=token_id, event=event)
        token.delete()
        return 204, None

    @route.get(
        "/tokens",
        url_name="list_event_tokens",
        response=PaginatedResponseSchema[schema.EventTokenSchema],
        throttle=UserDefaultThrottle(),
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    @searching(Searching, search_fields=["event__name", "event__description", "message"])
    def list_event_tokens(
        self,
        event_id: UUID,
        params: filters.EventTokenFilterSchema = Query(...),  # type: ignore[type-arg]
    ) -> QuerySet[models.EventToken]:
        """Retrieve all invitation tokens for this event with usage statistics.

        Event tokens serve two purposes:
        1. **Visibility** - Grant temporary access to view private events via `?et=` URL parameter
        2. **Invitations** - Allow users to claim invitations with optional ticket tier assignment

        Each token can have usage limits, expiration dates, and associated ticket tiers.

        **Returns:**
        Paginated list of tokens with:
        - `id`: The unique token code (used in shareable links and as `?et=` param)
        - `name`: Display name for organization
        - `issuer`: The user who created this token
        - `expires_at`: When the token stops working (null = never expires)
        - `uses`: How many times it's been claimed so far
        - `max_uses`: Maximum allowed claims (0 = unlimited)
        - `ticket_tier`: Which ticket tier users get when claiming
        - `invitation_payload`: Custom metadata (welcome message, flags, etc.)
        - `created_at`: When the token was created

        **Filtering & Search:**
        - Search by token name, event name/description, or custom message
        - Filter by expiration status, tier, or usage count (via params)
        - Results are paginated (20 per page by default)

        **Frontend Implementation:**
        Build a token management dashboard showing:
        1. **Token List Table:**
           - Token name (with copy-link button)
           - Usage: "23 / 50 uses" or "15 uses (unlimited)"
           - Status badge: "Active" (green), "Expired" (red), "Limit Reached" (yellow)
           - Tier name if specified
           - Expiration date
           - Actions: Edit, Delete, Copy Link

        2. **Shareable Link Format:**
           - For visibility: `https://yourapp.com/events/{event_id}?et={token_id}`
             (Frontend extracts `?et=` and sends as `X-Event-Token` header to API)
           - For claiming: `https://yourapp.com/invite/event/{token_id}`
             -> POST `/events/claim-invitation/{token_id}`

        3. **Status Indicators:**
           ```javascript
           function getTokenStatus(token) {
             if (token.expires_at && new Date(token.expires_at) < new Date()) return "Expired";
             if (token.max_uses > 0 && token.uses >= token.max_uses) return "Limit Reached";
             return "Active";
           }
           ```

        4. **Analytics Display:**
           - Show usage percentage as progress bar
           - Display "# claimed today" if tracking recent activity
           - Show which tier most users are getting

        **Use Cases:**
        - Display all invitation links in the event admin panel
        - Monitor token usage for capacity planning
        - Identify which promotional channels are most effective
        - Audit who created which tokens and when
        """
        self.get_one(event_id)
        return params.filter(models.EventToken.objects.filter(event_id=event_id)).distinct()

    @route.post(
        "/tokens",
        url_name="create_event_token",
        response=schema.EventTokenSchema,
    )
    def create_event_token(self, event_id: UUID, payload: schema.EventTokenCreateSchema) -> models.EventToken:
        """Create a new shareable token for this event.

        Event tokens serve dual purposes:
        1. **Primary: Visibility** - Share links like `/events/{id}?et={token}` to let non-members view private events
        2. **Secondary: Invitations** - Optionally allow users to claim event invitations with ticket tier assignment

        This enables sharing event details in group chats, social media, or with non-members without
        requiring them to join first.

        **Use Cases:**
        - **Social Media Promotions:** Share on Twitter/Instagram to let followers RSVP
        - **Email Campaigns:** Include in newsletters for easy one-click registration
        - **Partner Organizations:** Give to affiliated groups for member distribution
        - **Tiered Access:** Create different tokens for VIP, General, Student tiers
        - **Time-Limited Offers:** Early bird tokens that expire before price increase
        - **Capacity Management:** Tokens with usage limits (e.g., "50 from marketing list")
        - **Referral Tracking:** Create per-channel tokens to measure effectiveness

        **Parameters:**
        - `name`: Display name for organization (e.g., "Instagram Followers", "Alumni Network")
        - `duration`: Minutes until expiration (default: 24*60 = 1 day)
        - `max_uses`: Maximum claims allowed (default: 1, use 0 for unlimited)
        - `ticket_tier_id`: Ticket tier to auto-assign (required for ticketed events, optional otherwise)
        - `invitation`: Optional custom metadata:
          - `custom_message`: Personalized welcome text
          - Additional fields that your EventInvitation model supports

        **Returns:**
        The created token with a unique `id` that serves as the shareable code.

        **Business Logic:**
        - Token issuer is automatically set to the current authenticated user
        - Expiration is calculated from current time + duration
        - If ticket_tier_id provided, validates it belongs to this event
        - For ticketed events, ticket_tier_id is REQUIRED
        - Token ID is a secure random 8-character alphanumeric code
        - Created tokens start with 0 uses

        **Frontend Implementation:**
        After creation, immediately show the shareable link:

        ```javascript
        // On successful creation:
        const shareableUrl = `https://yourapp.com/invite/event/${response.id}`;

        // Show UI with:
        - Shareable link with copy button
        - QR code for in-person distribution
        - Social media share buttons
        - Usage tracking: "0 / {max_uses} used"
        - Expiration countdown: "Expires in 23 hours"
        ```

        **Example Workflow:**
        1. Organizer creates token with name="Instagram Post", max_uses=100, duration=7*24*60
        2. Frontend displays: `https://yourapp.com/invite/event/aBc12XyZ`
        3. Organizer shares link on Instagram
        4. Users visit link -> Frontend calls POST `/events/claim-invitation` -> Users get EventInvitation
        5. After 100 claims or 7 days, token becomes inactive

        **Error Cases:**
        - 400: ticket_tier_id missing for ticketed event, or tier doesn't belong to this event
        - 403: User lacks "invite_to_event" permission
        - 404: event_id not found or user lacks access
        """
        event = self.get_one(event_id)
        # Validate ticket_tier_id is required for ticketed events
        if event.requires_ticket and not payload.ticket_tier_id:
            raise HttpError(400, str(_("ticket_tier_id is required for events that require tickets.")))
        if payload.ticket_tier_id:
            get_object_or_404(models.TicketTier, pk=payload.ticket_tier_id, event=event)
        return event_service.create_event_token(event=event, issuer=self.user(), **payload.model_dump())

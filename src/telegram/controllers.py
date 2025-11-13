# src/telegram/controllers.py
"""Telegram API controllers."""

from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.schema import ResponseMessage
from common.throttling import UserDefaultThrottle, WriteThrottle
from telegram import service
from telegram.models import TelegramUser
from telegram.schema import BotNameSchema, TelegramLinkStatusSchema, TelegramOTPSchema


@api_controller("/telegram", tags=["Telegram"], auth=I18nJWTAuth(), throttle=UserDefaultThrottle())
class TelegramController(UserAwareController):
    """Controller for Telegram account linking."""

    @route.post(
        "/connect",
        response={200: ResponseMessage, 400: ResponseMessage},
        throttle=WriteThrottle(),
    )
    def connect_account(self, payload: TelegramOTPSchema) -> tuple[int, ResponseMessage]:
        """Link Telegram account using OTP from /connect command.

        Args:
            payload: OTP schema with 9-digit code.

        Returns:
            Success message if linking succeeds.

        Raises:
            HttpError: 400 if account already connected or OTP invalid/expired.
        """
        service.connect_accounts(self.user(), payload.cleaned_otp())
        return 200, ResponseMessage(message="Telegram account linked successfully")

    @route.post("/disconnect", response={200: None}, throttle=WriteThrottle())
    def disconnect_account(self) -> None:
        """Disconnect Telegram account from Revel user.

        Raises:
            HttpError: 400 if no Telegram account is linked.
        """
        service.disconnect_account(self.user())

    @route.get("/status", response=TelegramLinkStatusSchema)
    def get_link_status(self) -> TelegramLinkStatusSchema:
        """Check if user has linked Telegram account.

        Returns:
            Link status with connection state and username if connected.
        """
        tg_user = TelegramUser.objects.filter(user=self.user()).first()
        return TelegramLinkStatusSchema(
            connected=tg_user is not None, telegram_username=tg_user.telegram_username if tg_user else None
        )

    @route.get("/botname", response=BotNameSchema)
    def get_bot_name(self) -> BotNameSchema:
        """Get the Telegram bot name.

        Returns:
            Bot name retrieved from Telegram API (cached for 24 hours).
        """
        bot_name = service.get_bot_name()
        return BotNameSchema(botname=bot_name)

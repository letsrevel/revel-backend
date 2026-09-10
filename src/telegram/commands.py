from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeDefault

# Define the commands list (good practice to keep it organized)
DEFAULT_COMMANDS = [
    BotCommand(command="start", description="Start the bot / Show welcome message"),
    BotCommand(command="connect", description="🔗 Link your Revel account"),
    BotCommand(command="unsubscribe", description="🔕 Turn off all Telegram notifications"),
    BotCommand(command="cancel", description="🔙 Cancel the current action"),
    # Add other commands like /help if you implement them
    # BotCommand(command="help", description="Show help information"),
]


async def set_commands(bot: Bot) -> None:
    """Set the bot commands for the main menu.

    This function should be called automatically when the bot starts.
    """
    await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())

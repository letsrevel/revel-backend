# src/telegram/handlers/preferences.py

import structlog
from aiogram import Router

# Make sure DeliveryMethod is imported

logger = structlog.get_logger(__name__)
router = Router()

# --- Helper Functions ---

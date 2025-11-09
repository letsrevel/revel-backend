# src/telegram/tests/conftest.py
import random
import typing as t
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat
from aiogram.types import User as AiogramUser

from accounts.models import RevelUser
from events.models import Event, EventInvitation, Organization
from telegram.bot import get_dispatcher
from telegram.models import TelegramUser


@pytest.fixture
def bot() -> Bot:
    """Fixture for the aiogram Bot instance."""
    return Bot(token="test-token")


@pytest.fixture
def storage() -> MemoryStorage:
    """Fixture for in-memory FSM storage."""
    return MemoryStorage()


@pytest.fixture
def dispatcher(storage: MemoryStorage) -> Dispatcher:
    """Fixture for the aiogram Dispatcher."""
    return get_dispatcher(storage)


@pytest.fixture
def aiogram_user() -> AiogramUser:
    """Fixture for a standard Aiogram user."""
    return AiogramUser(
        id=random.randint(1, 1_000_000), is_bot=False, first_name="Test", last_name="User", username="testuser"
    )


@pytest.fixture
def aiogram_superuser(settings: MagicMock) -> AiogramUser:
    """Fixture for a superuser Aiogram user."""
    settings.TELEGRAM_SUPERUSER_IDS = [123]
    return AiogramUser(id=settings.TELEGRAM_SUPERUSER_IDS[0], is_bot=False, first_name="Super", last_name="User")


@pytest.fixture
def chat() -> Chat:
    """Fixture for an Aiogram chat object."""
    return Chat(id=54321, type="private")


@pytest_asyncio.fixture
@pytest.mark.django_db
async def django_user(aiogram_user: AiogramUser) -> t.AsyncIterator[RevelUser]:
    """Fixture for a standard Django user linked to a Telegram user."""
    user, _ = await RevelUser.objects.aget_or_create(
        username="testuser", defaults={"first_name": "Test", "last_name": "User", "password": "<PASSWORD>"}
    )
    await TelegramUser.objects.aget_or_create(user=user, telegram_id=aiogram_user.id)
    yield user
    await user.adelete()


@pytest_asyncio.fixture
@pytest.mark.django_db
async def django_superuser(aiogram_superuser: AiogramUser) -> t.AsyncIterator[RevelUser]:
    """Fixture for a Django superuser linked to a Telegram superuser."""
    user, _ = await RevelUser.objects.aget_or_create(
        username="super_user", defaults={"is_superuser": True, "is_staff": True, "password": "<PASSWORD>"}
    )
    await TelegramUser.objects.aget_or_create(user=user, telegram_id=aiogram_superuser.id)
    yield user
    await user.adelete()


@pytest_asyncio.fixture
@pytest.mark.django_db
async def django_inactive_user(aiogram_user: AiogramUser) -> t.AsyncIterator[RevelUser]:
    """Fixture for a Django inactive user linked to a Telegram user."""
    inactive_user = await RevelUser.objects.acreate(username="inactive", is_active=False, password="<PASSWORD>")
    await TelegramUser.objects.acreate(user=inactive_user, telegram_id=aiogram_user.id)
    yield inactive_user
    await inactive_user.adelete()


@pytest_asyncio.fixture
@pytest.mark.django_db
async def organization(django_superuser: RevelUser) -> t.AsyncIterator[Organization]:
    """Fixture for an Event organizer."""
    org = await Organization.objects.acreate(name="Test Organization", slug="test-org", owner=django_superuser)
    yield org
    await org.adelete()


@pytest_asyncio.fixture
async def private_event(organization: Organization) -> t.AsyncIterator[Event]:
    """Fixture for an Event."""
    event = await Event.objects.acreate(
        organization=organization, name="Test Event", slug="test-event", event_type=Event.EventType.PRIVATE
    )
    yield event
    await event.adelete()


@pytest_asyncio.fixture
@pytest.mark.django_db
async def event_invitation(django_user: RevelUser, private_event: Event) -> t.AsyncIterator[EventInvitation]:
    """Fixture for a Django event invitation linked to a Telegram user."""
    invitation = await EventInvitation.objects.acreate(event=private_event, user=django_user)
    yield invitation
    await invitation.adelete()

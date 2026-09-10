# src/telegram/fsm.py

from aiogram.fsm.state import State, StatesGroup


class BroadcastStates(StatesGroup):
    confirming_broadcast = State()

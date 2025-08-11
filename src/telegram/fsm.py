# src/telegram/fsm.py

from aiogram.fsm.state import State, StatesGroup


class PreferenceStates(StatesGroup):
    choosing_action = State()
    selecting_pref_for_action = State()  # For change/delete
    confirming_delete = State()

    # States for adding/changing a preference
    confirming_pref_save = State()


class BroadcastStates(StatesGroup):
    confirming_broadcast = State()

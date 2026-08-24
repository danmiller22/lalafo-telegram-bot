from aiogram.fsm.state import State, StatesGroup


class WantedAdForm(StatesGroup):
    rooms = State()
    district = State()
    budget = State()
    move_in = State()
    tenants = State()
    notes = State()
    contact = State()
    confirm = State()

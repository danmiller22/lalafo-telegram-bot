from aiogram.fsm.state import State, StatesGroup


class ApartmentMatchForm(StatesGroup):
    rooms = State()
    districts = State()
    budget = State()

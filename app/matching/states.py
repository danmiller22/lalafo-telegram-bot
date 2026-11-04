from aiogram.fsm.state import State, StatesGroup


class ApartmentMatchForm(StatesGroup):
    rooms = State()
    seller_types = State()
    districts = State()
    budget = State()

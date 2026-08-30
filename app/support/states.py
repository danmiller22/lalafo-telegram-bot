from aiogram.fsm.state import State, StatesGroup


class SupportConversation(StatesGroup):
    active = State()


class SupportAdminReply(StatesGroup):
    writing = State()

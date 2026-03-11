"""
Состояния FSM (Finite State Machine)
"""
from aiogram.fsm.state import State, StatesGroup


class BookingStates(StatesGroup):
    """Состояния бронирования"""
    selecting_date = State()
    selecting_time = State()
    selecting_format = State()
    entering_content = State()
    confirming = State()
    uploading_payment = State()


class AdminChannelStates(StatesGroup):
    """Состояния добавления/редактирования канала"""
    waiting_channel_forward = State()
    waiting_price = State()  # Универсальное состояние
    waiting_category = State()
    waiting_manual_subscribers = State()
    waiting_manual_reach = State()
    waiting_manual_err = State()
    waiting_cpm = State()  # Ручной ввод CPM для канала


class AdminCPMStates(StatesGroup):
    """Состояния ручного ввода CPM по тематикам"""
    waiting_cpm_value = State()


class AdminAutopostingStates(StatesGroup):
    """Состояния управления автопостингом"""
    waiting_post_views = State()
    waiting_post_reactions = State()
    waiting_post_forwards = State()
    waiting_post_saves = State()


class ManagerStates(StatesGroup):
    """Состояния менеджера"""
    viewing_lesson = State()
    taking_quiz = State()
    ai_conversation = State()
    payout_amount = State()
    payout_method = State()
    payout_details = State()


class AdminPasswordState(StatesGroup):
    """Состояние ввода пароля админа"""
    waiting_admin_password = State()


class AdminCompetitionStates(StatesGroup):
    """Состояния создания соревнования"""
    waiting_name = State()
    waiting_start_date = State()
    waiting_end_date = State()
    waiting_prize_pool = State()
    waiting_metric = State()

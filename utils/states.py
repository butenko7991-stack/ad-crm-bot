"""
Состояния FSM (Finite State Machine)
"""
from aiogram.fsm.state import State, StatesGroup


class BookingStates(StatesGroup):
    """Состояния бронирования"""
    selecting_channel = State()
    selecting_date = State()
    selecting_time = State()
    selecting_format = State()
    entering_content = State()
    uploading_media = State()
    confirming = State()
    uploading_payment = State()


class AdminChannelStates(StatesGroup):
    """Состояния добавления/редактирования канала"""
    waiting_channel_forward = State()
    waiting_channel_name = State()
    waiting_price_1_24 = State()
    waiting_price_1_48 = State()
    waiting_price_2_48 = State()
    waiting_price_native = State()
    waiting_price = State()  # Универсальное состояние
    waiting_category = State()
    waiting_manual_subscribers = State()
    waiting_manual_reach = State()
    waiting_manual_err = State()


class ManagerStates(StatesGroup):
    """Состояния менеджера"""
    in_training = State()
    viewing_lesson = State()
    taking_quiz = State()
    ai_conversation = State()
    payout_amount = State()
    payout_method = State()
    payout_details = State()


class AdminPasswordState(StatesGroup):
    """Состояние ввода пароля админа"""
    waiting_admin_password = State()


class AutopostStates(StatesGroup):
    """Состояния автопостинга"""
    selecting_channel = State()
    entering_content = State()
    uploading_media = State()
    selecting_time = State()
    confirming = State()

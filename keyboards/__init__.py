"""
Keyboards package
"""
from keyboards.menus import (
    get_main_menu, get_admin_panel_menu, get_manager_cabinet_menu,
    get_channels_keyboard, get_channel_settings_keyboard, get_category_keyboard,
    get_dates_keyboard, get_times_keyboard, get_format_keyboard,
    get_training_menu, get_ai_feedback_keyboard,
    get_payout_keyboard, get_back_keyboard, get_confirm_keyboard
)

__all__ = [
    "get_main_menu", "get_admin_panel_menu", "get_manager_cabinet_menu",
    "get_channels_keyboard", "get_channel_settings_keyboard", "get_category_keyboard",
    "get_dates_keyboard", "get_times_keyboard", "get_format_keyboard",
    "get_training_menu", "get_ai_feedback_keyboard",
    "get_payout_keyboard", "get_back_keyboard", "get_confirm_keyboard"
]

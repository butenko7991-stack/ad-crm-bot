"""
Клавиатуры и меню
"""
from typing import List, Optional
from datetime import date

from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

from config import CHANNEL_CATEGORIES, MANAGER_LEVELS


# ==================== ГЛАВНЫЕ МЕНЮ ====================

def get_main_menu(
    is_admin: bool = False, 
    is_authenticated_admin: bool = False,
    is_manager: bool = False
) -> ReplyKeyboardMarkup:
    """Главное меню в зависимости от роли"""
    
    if is_authenticated_admin:
        # Авторизованный админ
        buttons = [
            [KeyboardButton(text="📢 Каналы"), KeyboardButton(text="💳 Оплаты")],
            [KeyboardButton(text="👥 Менеджеры"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📝 Модерация"), KeyboardButton(text="🏆 Лидерборд")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🚪 Выйти")]
        ]
    elif is_manager:
        # Менеджер
        buttons = [
            [KeyboardButton(text="📚 Обучение"), KeyboardButton(text="💼 Продажи")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="📋 Шаблоны"), KeyboardButton(text="🏆 Рейтинг")]
        ]
    elif is_admin:
        # Админ не авторизован
        buttons = [
            [KeyboardButton(text="🔐 Войти в админку")],
            [KeyboardButton(text="📢 Каталог каналов")]
        ]
    else:
        # Обычный клиент
        buttons = [
            [KeyboardButton(text="📢 Каталог каналов")],
            [KeyboardButton(text="📦 Мои заказы")],
            [KeyboardButton(text="💼 Стать менеджером")]
        ]
    
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True
    )


def get_admin_panel_menu() -> InlineKeyboardMarkup:
    """Меню админ-панели"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📢 Каналы", callback_data="adm_channels"),
            InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")
        ],
        [
            InlineKeyboardButton(text="💳 Оплаты", callback_data="adm_payments"),
            InlineKeyboardButton(text="📝 Модерация", callback_data="adm_moderation")
        ],
        [
            InlineKeyboardButton(text="👥 Менеджеры", callback_data="adm_managers"),
            InlineKeyboardButton(text="💸 Выплаты", callback_data="adm_payouts")
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats"),
            InlineKeyboardButton(text="🏆 Соревнования", callback_data="adm_competitions")
        ],
        [
            InlineKeyboardButton(text="💰 CPM тематик", callback_data="adm_cpm"),
            InlineKeyboardButton(text="⚙️ Настройки бота", callback_data="adm_settings")
        ],
    ])


def get_manager_cabinet_menu() -> InlineKeyboardMarkup:
    """Меню кабинета менеджера"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Мои продажи", callback_data="mgr_my_sales"),
            InlineKeyboardButton(text="👥 Мои клиенты", callback_data="mgr_my_clients")
        ],
        [
            InlineKeyboardButton(text="📋 Шаблоны", callback_data="mgr_templates"),
            InlineKeyboardButton(text="🤖 AI-помощник", callback_data="ai_trainer")
        ],
        [
            InlineKeyboardButton(text="💰 Вывод средств", callback_data="request_payout"),
            InlineKeyboardButton(text="🏆 Рейтинг", callback_data="mgr_leaderboard")
        ],
        [InlineKeyboardButton(text="🔗 Моя реф-ссылка", callback_data="copy_ref_link")]
    ])


# ==================== КАНАЛЫ ====================

def get_channels_keyboard(channels: list) -> InlineKeyboardMarkup:
    """Клавиатура списка каналов"""
    buttons = []
    for ch in channels:
        prices = ch.get("prices", {}) if isinstance(ch, dict) else (ch.prices or {})
        price_124 = prices.get("1/24", 0)
        name = ch.get("name", "") if isinstance(ch, dict) else ch.name
        ch_id = ch.get("id", 0) if isinstance(ch, dict) else ch.id
        
        buttons.append([InlineKeyboardButton(
            text=f"📢 {name} — от {price_124:,}₽",
            callback_data=f"channel:{ch_id}"
        )])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_channel_settings_keyboard(channel_id: int, is_active: bool) -> InlineKeyboardMarkup:
    """Клавиатура настроек канала"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Обновить статистику", callback_data=f"adm_ch_update:{channel_id}")],
        [InlineKeyboardButton(text="💰 Изменить цены", callback_data=f"adm_ch_prices:{channel_id}")],
        [InlineKeyboardButton(text="📅 Управление слотами", callback_data=f"adm_ch_slots:{channel_id}")],
        [InlineKeyboardButton(
            text="❌ Деактивировать" if is_active else "✅ Активировать",
            callback_data=f"adm_ch_toggle:{channel_id}"
        )],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_ch_delete:{channel_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_channels")]
    ])


def get_category_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора категории"""
    buttons = []
    row = []
    
    for key, cat in CHANNEL_CATEGORIES.items():
        row.append(InlineKeyboardButton(
            text=cat["name"],
            callback_data=f"cat:{key}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    
    if row:
        buttons.append(row)
    
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== БРОНИРОВАНИЕ ====================

def get_dates_keyboard(slots: list) -> InlineKeyboardMarkup:
    """Клавиатура выбора даты"""
    dates = sorted(set(s.slot_date for s in slots))
    buttons = []
    row = []
    
    for d in dates[:14]:
        row.append(InlineKeyboardButton(
            text=d.strftime("%d.%m"),
            callback_data=f"date:{d.isoformat()}"
        ))
        if len(row) == 4:
            buttons.append(row)
            row = []
    
    if row:
        buttons.append(row)
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_channels")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_times_keyboard(slots: list, channel_prices: dict) -> InlineKeyboardMarkup:
    """Клавиатура выбора времени"""
    buttons = []
    
    for slot in slots:
        time_str = slot.slot_time.strftime("%H:%M")
        buttons.append([InlineKeyboardButton(
            text=f"🕐 {time_str}",
            callback_data=f"time:{slot.id}"
        )])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_dates")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_format_keyboard(channel_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора формата размещения"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1/24 (24ч)", callback_data="format:1/24"),
            InlineKeyboardButton(text="1/48 (48ч)", callback_data="format:1/48")
        ],
        [
            InlineKeyboardButton(text="2/48 (2 поста)", callback_data="format:2/48"),
            InlineKeyboardButton(text="Навсегда", callback_data="format:native")
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_times")]
    ])


# ==================== ОБУЧЕНИЕ ====================

def get_training_menu() -> InlineKeyboardMarkup:
    """Меню обучения"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📖 Уроки", callback_data="show_lessons")],
        [InlineKeyboardButton(text="✅ Пройденные", callback_data="completed_lessons")],
        [InlineKeyboardButton(text="🤖 AI-тренер", callback_data="ai_trainer")],
        [InlineKeyboardButton(text="📊 Мой прогресс", callback_data="training_progress")]
    ])


def get_ai_feedback_keyboard() -> InlineKeyboardMarkup:
    """Кнопки обратной связи для AI"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍 Полезно", callback_data="ai_feedback:helpful"),
            InlineKeyboardButton(text="👎 Не понял", callback_data="ai_feedback:not_helpful")
        ]
    ])


# ==================== ВЫПЛАТЫ ====================

def get_payout_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура способов выплаты"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Карта", callback_data="payout:card"),
            InlineKeyboardButton(text="📱 СБП", callback_data="payout:sbp")
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])


# ==================== ОБЩИЕ ====================

def get_back_keyboard(callback_data: str = "cancel") -> InlineKeyboardMarkup:
    """Кнопка назад"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)]
    ])


def get_confirm_keyboard(confirm_data: str, cancel_data: str = "cancel") -> InlineKeyboardMarkup:
    """Кнопки подтверждения"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data=confirm_data),
            InlineKeyboardButton(text="❌ Нет", callback_data=cancel_data)
        ]
    ])

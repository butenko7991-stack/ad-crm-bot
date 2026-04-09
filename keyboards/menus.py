"""
Клавиатуры и меню
"""
import calendar
from typing import List, Optional, Dict
from datetime import date, timedelta

from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

from config import CHANNEL_CATEGORIES, MANAGER_LEVELS, AVAILABLE_TIMEZONES

# Локализованные названия месяцев (именительный падеж)
_MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
]

# Сокращённые названия дней недели (Пн–Вс)
_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ==================== ГЛАВНЫЕ МЕНЮ ====================

def get_role_selection_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора роли при первом запуске"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Закупщик", callback_data="set_role:buyer")],
        [InlineKeyboardButton(text="✍️ Контенщик", callback_data="set_role:content")],
        [InlineKeyboardButton(text="💼 Менеджер по продажам", callback_data="set_role:manager")],
    ])


def get_main_menu(
    is_admin: bool = False,
    is_authenticated_admin: bool = False,
    is_manager: bool = False,
    manager_role: str = None,
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
    elif is_manager and manager_role == "buyer":
        # Закупщик — разделы, связанные с закупом рекламы
        buttons = [
            [KeyboardButton(text="📢 Каталог каналов"), KeyboardButton(text="📊 Аналитика каналов")],
            [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="🔄 Сменить роль")],
        ]
    elif is_manager and manager_role == "content":
        # Контенщик — разделы, связанные с постингом
        buttons = [
            [KeyboardButton(text="📅 Мои посты"), KeyboardButton(text="✍️ Подать пост")],
            [KeyboardButton(text="📈 Аналитика постов"), KeyboardButton(text="🤖 Автопостинг")],
            [KeyboardButton(text="📋 Контент план"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🔄 Сменить роль")],
        ]
    elif is_manager:
        # Менеджер по продажам (роль 'manager' или без роли)
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
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats")
        ],
        [
            InlineKeyboardButton(text="🏆 Соревнования", callback_data="adm_competitions"),
            InlineKeyboardButton(text="💰 CPM тематик", callback_data="adm_cpm")
        ],
        [
            InlineKeyboardButton(text="📅 Автопостинг", callback_data="adm_autoposting"),
            InlineKeyboardButton(text="⚙️ Настройки бота", callback_data="adm_settings")
        ],
        [
            InlineKeyboardButton(text="🔧 Диагностика", callback_data="adm_diagnostics"),
            InlineKeyboardButton(text="🤖 AI-улучшения", callback_data="adm_ai_improve"),
        ],
        [
            InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm_promo"),
            InlineKeyboardButton(text="📅 Расписание дня", callback_data="adm_send_daily_schedule"),
        ],
        [
            InlineKeyboardButton(text="📋 Контент план", callback_data="adm_content_plan"),
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
        [
            InlineKeyboardButton(text="📝 Подать пост на модерацию", callback_data="mgr_submit_post"),
            InlineKeyboardButton(text="📋 Мои посты", callback_data="mgr_my_posts"),
        ],
        [
            InlineKeyboardButton(text="🔗 Моя реф-ссылка", callback_data="copy_ref_link"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="mgr_settings"),
        ],
    ])


def get_timezone_keyboard(current_offset: int = 3, context: str = "settings") -> InlineKeyboardMarkup:
    """Клавиатура выбора часового пояса.

    context: 'register' — для регистрации, 'settings' — для смены в кабинете.
    Callback data: mgr_tz_{context}:{offset}
    """
    buttons = []
    for offset, label in AVAILABLE_TIMEZONES:
        mark = "✅ " if offset == current_offset else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"mgr_tz_{context}:{offset}"
        )])
    if context == "settings":
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== МЕТРИКИ ====================

def get_metrics_menu() -> InlineKeyboardMarkup:
    """Навигационное меню раздела метрик"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Продажи", callback_data="metrics_sales:month"),
            InlineKeyboardButton(text="📢 Каналы", callback_data="metrics_channels"),
        ],
        [
            InlineKeyboardButton(text="👥 Менеджеры", callback_data="metrics_managers"),
            InlineKeyboardButton(text="🧑‍💼 Клиенты", callback_data="metrics_clients"),
        ],
        [
            InlineKeyboardButton(text="🗂 Форматы", callback_data="metrics_formats"),
            InlineKeyboardButton(text="📈 Посты", callback_data="metrics_posts"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])


def get_sales_period_keyboard(active: str = "month") -> InlineKeyboardMarkup:
    """Переключатель периода для метрик продаж"""
    def _btn(label: str, period: str) -> InlineKeyboardButton:
        prefix = "✅ " if active == period else ""
        return InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"metrics_sales:{period}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("День", "day"), _btn("Неделя", "week"), _btn("Месяц", "month")],
        [InlineKeyboardButton(text="◀️ К метрикам", callback_data="adm_stats")],
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
        [InlineKeyboardButton(text="📈 Аналитика канала", callback_data=f"ch_analytics:{channel_id}")],
        [InlineKeyboardButton(text="💰 Изменить цены", callback_data=f"adm_ch_prices:{channel_id}")],
        [InlineKeyboardButton(text="🔗 Изменить ссылку", callback_data=f"adm_ch_set_link:{channel_id}")],
        [InlineKeyboardButton(text="📅 Слоты", callback_data=f"adm_ch_slots:{channel_id}")],
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

def get_calendar_keyboard(
    slots: list,
    year: int,
    month: int,
    back_cb: str = "back_to_channels",
    date_cb_prefix: str = "date",
    nav_cb_prefix: str = "cal_nav",
) -> InlineKeyboardMarkup:
    """Календарь-клавиатура с подсвеченными доступными датами.

    Аргументы:
        slots          — список объектов Slot (должны иметь поле slot_date: date)
        year, month    — отображаемый месяц
        back_cb        — callback_data кнопки «Назад»
        date_cb_prefix — префикс callback_data при выборе даты (``date`` или ``mgr_post_date``)
        nav_cb_prefix  — префикс callback_data для навигации по месяцам
    """
    today = date.today()

    # Набор дат, для которых есть доступные слоты
    available_dates = {s.slot_date for s in slots}

    buttons: list = []

    # ── Строка навигации: ◀️  Месяц ГГГГ  ▶️ ──────────────────────────
    prev_month = month - 1 if month > 1 else 12
    prev_year  = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year  = year if month < 12 else year + 1

    header_text = f"{_MONTH_NAMES[month]} {year}"
    buttons.append([
        InlineKeyboardButton(text="◀️", callback_data=f"{nav_cb_prefix}:{prev_year}:{prev_month}"),
        InlineKeyboardButton(text=header_text, callback_data="cal_ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"{nav_cb_prefix}:{next_year}:{next_month}"),
    ])

    # ── Заголовок дней недели ──────────────────────────────────────────
    buttons.append([
        InlineKeyboardButton(text=d, callback_data="cal_ignore") for d in _WEEKDAYS
    ])

    # ── Дни месяца ────────────────────────────────────────────────────
    cal = calendar.monthcalendar(year, month)
    for week in cal:
        row = []
        for weekday, day in enumerate(week):
            if day == 0:
                # Пустая ячейка (до начала / после конца месяца)
                row.append(InlineKeyboardButton(text=" ", callback_data="cal_ignore"))
            else:
                current = date(year, month, day)
                if current in available_dates and current >= today:
                    # День со слотами — можно выбрать
                    row.append(InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"{date_cb_prefix}:{current.isoformat()}"
                    ))
                else:
                    # Нет слотов или прошедший день
                    row.append(InlineKeyboardButton(text="·", callback_data="cal_ignore"))
        buttons.append(row)

    # ── Кнопка «Назад» ────────────────────────────────────────────────
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_dates_keyboard(slots: list) -> InlineKeyboardMarkup:
    """Клавиатура выбора даты (отображает текущий месяц с доступными датами)."""
    today = date.today()
    return get_calendar_keyboard(slots, today.year, today.month)


def get_free_calendar_keyboard(
    year: int,
    month: int,
    back_cb: str = "adm_autoposting",
    date_cb_prefix: str = "autopost_cal_date",
    nav_cb_prefix: str = "autopost_cal_nav",
    publish_now_cb: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """Свободный календарь для выбора даты публикации (без привязки к слотам).

    Все даты начиная с сегодняшнего дня доступны для выбора.
    Сегодняшняя дата выделяется маркером 🔹.
    Прошедшие даты отображаются как «·».
    """
    today = date.today()

    buttons: list = []

    # ── Строка навигации: ◀️  Месяц ГГГГ  ▶️ ──────────────────────────
    prev_month = month - 1 if month > 1 else 12
    prev_year  = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year  = year if month < 12 else year + 1

    header_text = f"{_MONTH_NAMES[month]} {year}"
    # Не позволяем уходить в прошлое дальше текущего месяца
    prev_cb = (
        f"{nav_cb_prefix}:{prev_year}:{prev_month}"
        if (prev_year, prev_month) >= (today.year, today.month)
        else "cal_ignore"
    )
    buttons.append([
        InlineKeyboardButton(text="◀️", callback_data=prev_cb),
        InlineKeyboardButton(text=header_text, callback_data="cal_ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"{nav_cb_prefix}:{next_year}:{next_month}"),
    ])

    # ── Заголовок дней недели ──────────────────────────────────────────
    buttons.append([
        InlineKeyboardButton(text=d, callback_data="cal_ignore") for d in _WEEKDAYS
    ])

    # ── Дни месяца ────────────────────────────────────────────────────
    cal = calendar.monthcalendar(year, month)
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="cal_ignore"))
            else:
                current = date(year, month, day)
                if current < today:
                    # Прошедший день
                    row.append(InlineKeyboardButton(text="·", callback_data="cal_ignore"))
                elif current == today:
                    # Сегодня — выделяем маркером
                    row.append(InlineKeyboardButton(
                        text=f"🔹{day}",
                        callback_data=f"{date_cb_prefix}:{current.isoformat()}"
                    ))
                else:
                    # Будущий день — доступен
                    row.append(InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"{date_cb_prefix}:{current.isoformat()}"
                    ))
        buttons.append(row)

    # ── Кнопка «Опубликовать сейчас» (опциональная) ───────────────────
    if publish_now_cb:
        buttons.append([InlineKeyboardButton(text="📤 Опубликовать сейчас", callback_data=publish_now_cb)])

    # ── Кнопка «Назад» (get_free_calendar_keyboard) ───────────────────
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_time_picker_keyboard(
    selected_date_iso: str,
    back_cb: str = "autopost_cal_back",
    time_cb_prefix: str = "autopost_time",
) -> InlineKeyboardMarkup:
    """Клавиатура выбора времени публикации (часовые слоты с 07:00 до 22:00).

    Callback data формат: ``{prefix}:{YYYY-MM-DD}:{HHMM}`` (время без двоеточия).
    """
    hours = [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
    buttons: list = []
    row: list = []
    for h in hours:
        display = f"{h:02d}:00"
        cb_time = f"{h:02d}00"  # без двоеточия, чтобы не ломать split по ":"
        row.append(InlineKeyboardButton(
            text=display,
            callback_data=f"{time_cb_prefix}:{selected_date_iso}:{cb_time}"
        ))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_times_keyboard(slots: list) -> InlineKeyboardMarkup:
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


# ==================== CPM ====================

def get_cpm_categories_keyboard(categories: list, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    """Клавиатура списка CPM-тематик с кнопками редактирования"""
    start = page * per_page
    end = start + per_page
    page_cats = categories[start:end]

    buttons = []
    for key, cat in page_cats:
        buttons.append([
            InlineKeyboardButton(
                text=f"{cat['name']}: {cat['cpm']:,}₽",
                callback_data=f"cpm_info:{key}"
            ),
            InlineKeyboardButton(text="✏️", callback_data=f"cpm_edit:{key}")
        ])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"cpm_page:{page - 1}"))
    if end < len(categories):
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"cpm_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== АВТОПОСТИНГ ====================

def get_autoposting_menu() -> InlineKeyboardMarkup:
    """Меню раздела Автопостинг"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать пост", callback_data="autopost_create")],
        [
            InlineKeyboardButton(text="📋 Запланированные", callback_data="autopost_pending"),
            InlineKeyboardButton(text="✅ Опубликованные", callback_data="autopost_posted"),
        ],
        [
            InlineKeyboardButton(text="📊 Аналитика постов", callback_data="autopost_analytics"),
            InlineKeyboardButton(text="🤖 AI-рекомендации", callback_data="autopost_ai_recommend"),
        ],
        [InlineKeyboardButton(text="📈 Аналитика каналов", callback_data="autopost_channel_analytics")],
        [InlineKeyboardButton(text="👁 Охваты за сутки", callback_data="daily_reach_report")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
    ])


def get_post_analytics_keyboard(analytics_list: list) -> InlineKeyboardMarkup:
    """Клавиатура списка аналитики постов"""
    buttons = []
    for item in analytics_list[:10]:
        views = item.views or 0
        reactions = item.reactions or 0
        forwards = item.forwards or 0
        has_metrics = views > 0 or reactions > 0 or forwards > 0
        status = "📊" if has_metrics else "➕"
        label = f"{status} #{item.id} — 👁{views} 👍{reactions} ↩️{forwards}"
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"pa_view:{item.id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_post_analytics_actions_keyboard(analytics_id: int, has_ai: bool = False, scheduled_post_id: int = None) -> InlineKeyboardMarkup:
    """Кнопки действий для записи аналитики поста"""
    buttons = []
    if scheduled_post_id:
        buttons.append([InlineKeyboardButton(
            text="📝 Внести/обновить метрики",
            callback_data=f"pa_enter:{scheduled_post_id}"
        )])
    if not has_ai:
        buttons.append([InlineKeyboardButton(
            text="🤖 Получить AI-рекомендации",
            callback_data=f"pa_ai:{analytics_id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="autopost_analytics")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ==================== КОНТЕНТ ПЛАН ====================

def get_content_plan_week_keyboard(
    week_monday: date,
    posts_by_date: Dict[date, int],
    back_cb: str = "adm_back",
) -> InlineKeyboardMarkup:
    """Клавиатура недельного контент-плана.

    week_monday      — первый день (понедельник) отображаемой недели.
    posts_by_date    — словарь {дата: количество запланированных постов}.
    back_cb          — callback_data кнопки «Назад».
    """
    today = date.today()
    week_sunday = week_monday + timedelta(days=6)

    prev_monday = week_monday - timedelta(days=7)
    next_monday = week_monday + timedelta(days=7)

    buttons: list = []

    # ── Строка навигации: ◀️ Неделя ДД.ММ – ДД.ММ ▶️ ─────────────────
    # Запрещаем уходить в прошлое дальше текущей недели
    current_week_monday = today - timedelta(days=today.weekday())
    prev_cb = (
        f"content_plan_week:{prev_monday.isoformat()}"
        if prev_monday >= current_week_monday
        else "cal_ignore"
    )
    header = f"{week_monday.strftime('%d.%m')} – {week_sunday.strftime('%d.%m.%Y')}"
    buttons.append([
        InlineKeyboardButton(text="◀️", callback_data=prev_cb),
        InlineKeyboardButton(text=header, callback_data="cal_ignore"),
        InlineKeyboardButton(text="▶️", callback_data=f"content_plan_week:{next_monday.isoformat()}"),
    ])

    # ── Дни недели ────────────────────────────────────────────────────
    for i in range(7):
        d = week_monday + timedelta(days=i)
        day_label = _WEEKDAYS[i]
        count = posts_by_date.get(d, 0)
        today_marker = " 🔹" if d == today else ""

        if count > 0:
            n = count % 10
            n100 = count % 100
            if n == 1 and n100 != 11:
                suffix = ""
            elif 2 <= n <= 4 and n100 not in (12, 13, 14):
                suffix = "а"
            else:
                suffix = "ов"
            text = f"{day_label} {d.strftime('%d.%m')}{today_marker} — {count} пост{suffix}"
            buttons.append([InlineKeyboardButton(
                text=text,
                callback_data=f"content_plan_day:{d.isoformat()}"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text=f"{day_label} {d.strftime('%d.%m')}{today_marker} — нет постов",
                callback_data="cal_ignore"
            )])

    # ── Кнопки перехода к сегодня / назад ─────────────────────────────
    nav_row = []
    if week_monday != current_week_monday:
        nav_row.append(InlineKeyboardButton(
            text="📅 Текущая неделя",
            callback_data=f"content_plan_week:{current_week_monday.isoformat()}"
        ))
    nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb))
    buttons.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_content_plan_day_keyboard(
    day: date,
    back_cb: str = "adm_content_plan",
) -> InlineKeyboardMarkup:
    """Клавиатура для детального просмотра постов на конкретный день."""
    week_monday = day - timedelta(days=day.weekday())
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="◀️ К неделе",
            callback_data=f"content_plan_week:{week_monday.isoformat()}"
        )],
        [InlineKeyboardButton(text="◀️ Контент план", callback_data=back_cb)],
    ])

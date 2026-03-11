"""
Клавиатуры для бота в сети Max
"""
from maxapi.types import CallbackButton, LinkButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from config import CHANNEL_CATEGORIES


# ==================== ГЛАВНЫЕ МЕНЮ ====================

def get_main_menu_markup(
    is_admin: bool = False,
    is_authenticated_admin: bool = False,
    is_manager: bool = False,
) -> list:
    """Возвращает список вложений (клавиатуру) для главного меню Max."""
    builder = InlineKeyboardBuilder()

    if is_authenticated_admin:
        builder.row(
            CallbackButton(text="📢 Каналы", payload="adm_channels"),
            CallbackButton(text="💳 Оплаты", payload="adm_payments"),
        )
        builder.row(
            CallbackButton(text="👥 Менеджеры", payload="adm_managers"),
            CallbackButton(text="📊 Статистика", payload="adm_stats"),
        )
        builder.row(
            CallbackButton(text="📝 Модерация", payload="adm_moderation"),
            CallbackButton(text="🏆 Лидерборд", payload="adm_leaderboard"),
        )
        builder.row(
            CallbackButton(text="⚙️ Настройки", payload="adm_settings"),
            CallbackButton(text="🚪 Выйти", payload="adm_logout"),
        )
    elif is_manager:
        builder.row(
            CallbackButton(text="📚 Обучение", payload="training"),
            CallbackButton(text="💼 Продажи", payload="sales"),
        )
        builder.row(
            CallbackButton(text="👤 Профиль", payload="profile"),
            CallbackButton(text="💰 Баланс", payload="balance"),
        )
        builder.row(
            CallbackButton(text="📋 Шаблоны", payload="templates"),
            CallbackButton(text="🏆 Рейтинг", payload="leaderboard"),
        )
    elif is_admin:
        builder.row(CallbackButton(text="🔐 Войти в админку", payload="admin_login"))
        builder.row(CallbackButton(text="📢 Каталог каналов", payload="catalog"))
    else:
        builder.row(CallbackButton(text="📢 Каталог каналов", payload="catalog"))
        builder.row(CallbackButton(text="📦 Мои заказы", payload="my_orders"))
        builder.row(CallbackButton(text="💼 Стать менеджером", payload="become_manager"))

    return [builder.as_markup()]


def get_channels_markup(channels: list) -> list:
    """Клавиатура списка каналов."""
    builder = InlineKeyboardBuilder()
    for ch in channels:
        prices = ch.get("prices", {}) if isinstance(ch, dict) else (ch.prices or {})
        price_124 = prices.get("1/24", 0)
        name = ch.get("name", "") if isinstance(ch, dict) else ch.name
        ch_id = ch.get("id", 0) if isinstance(ch, dict) else ch.id
        builder.row(CallbackButton(
            text=f"📢 {name} — от {price_124:,}₽",
            payload=f"channel:{ch_id}",
        ))
    return [builder.as_markup()]


def get_channel_detail_markup(channel_id: int) -> list:
    """Кнопки детального просмотра канала."""
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="📅 Забронировать", payload=f"book_channel:{channel_id}"))
    builder.row(CallbackButton(text="◀️ Назад", payload="catalog"))
    return [builder.as_markup()]


def get_admin_panel_markup() -> list:
    """Меню админ-панели."""
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="📢 Каналы", payload="adm_channels"),
        CallbackButton(text="➕ Добавить", payload="adm_add_channel"),
    )
    builder.row(
        CallbackButton(text="💳 Оплаты", payload="adm_payments"),
        CallbackButton(text="📝 Модерация", payload="adm_moderation"),
    )
    builder.row(
        CallbackButton(text="👥 Менеджеры", payload="adm_managers"),
        CallbackButton(text="📊 Статистика", payload="adm_stats"),
    )
    return [builder.as_markup()]


def get_manager_cabinet_markup() -> list:
    """Меню кабинета менеджера."""
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="📊 Мои продажи", payload="mgr_my_sales"),
        CallbackButton(text="👥 Мои клиенты", payload="mgr_my_clients"),
    )
    builder.row(
        CallbackButton(text="📋 Шаблоны", payload="mgr_templates"),
        CallbackButton(text="🤖 AI-помощник", payload="ai_trainer"),
    )
    builder.row(
        CallbackButton(text="💰 Вывод средств", payload="request_payout"),
        CallbackButton(text="🏆 Рейтинг", payload="mgr_leaderboard"),
    )
    builder.row(CallbackButton(text="🔗 Моя реф-ссылка", payload="copy_ref_link"))
    return [builder.as_markup()]


def get_training_markup() -> list:
    """Меню обучения."""
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="📖 Уроки", payload="show_lessons"))
    builder.row(CallbackButton(text="✅ Пройденные", payload="completed_lessons"))
    builder.row(CallbackButton(text="🤖 AI-тренер", payload="ai_trainer"))
    builder.row(CallbackButton(text="📊 Мой прогресс", payload="training_progress"))
    return [builder.as_markup()]


def get_become_manager_markup() -> list:
    """Кнопки для регистрации менеджером."""
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="✅ Стать менеджером", payload="manager_register"),
        CallbackButton(text="❌ Отмена", payload="cancel"),
    )
    return [builder.as_markup()]


def get_payout_markup(has_balance: bool) -> list:
    """Кнопки раздела баланса."""
    builder = InlineKeyboardBuilder()
    if has_balance:
        builder.row(CallbackButton(text="💸 Вывести", payload="request_payout"))
    builder.row(CallbackButton(text="📜 История выплат", payload="payout_history"))
    return [builder.as_markup()]


def get_confirm_markup(confirm_payload: str, cancel_payload: str = "cancel") -> list:
    """Кнопки подтверждения."""
    builder = InlineKeyboardBuilder()
    builder.row(
        CallbackButton(text="✅ Да", payload=confirm_payload),
        CallbackButton(text="❌ Нет", payload=cancel_payload),
    )
    return [builder.as_markup()]


def get_back_markup(payload: str = "cancel") -> list:
    """Кнопка назад."""
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="◀️ Назад", payload=payload))
    return [builder.as_markup()]


def get_admin_login_markup() -> list:
    """Кнопка входа в админку."""
    builder = InlineKeyboardBuilder()
    builder.row(CallbackButton(text="🔑 Войти", payload="request_admin_password"))
    return [builder.as_markup()]

"""
Вспомогательные функции
"""
import logging
from typing import Optional
from datetime import datetime, timezone, timedelta

from aiogram import Bot

from config import CHANNEL_CATEGORIES, MSK_OFFSET


logger = logging.getLogger(__name__)


# ─── Московское время (UTC+3) ─────────────────────────────────────────────────

def utc_now() -> datetime:
    """Текущий момент UTC как naive datetime (замена устаревшего datetime.utcnow())."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def msk_now() -> datetime:
    """Текущий момент по московскому времени (UTC+3) как naive datetime."""
    return utc_now() + MSK_OFFSET


def to_utc(msk_dt: datetime) -> datetime:
    """Перевести naive московское время → naive UTC."""
    return msk_dt - MSK_OFFSET


def to_msk(utc_dt: datetime) -> datetime:
    """Перевести naive UTC → naive московское время."""
    return utc_dt + MSK_OFFSET


async def get_channel_stats_via_bot(bot: Bot, channel_id: int) -> Optional[dict]:
    """
    Получить статистику канала через Bot API.
    Бот должен быть админом канала.
    """
    try:
        chat = await bot.get_chat(channel_id)
        member_count = await bot.get_chat_member_count(channel_id)
        
        return {
            "title": chat.title,
            "username": chat.username,
            "description": chat.description,
            "subscribers": member_count
        }
    except Exception as e:
        logger.error(f"Error getting channel stats: {e}")
        return None


def _apply_err_adjustment(price: float, err_percent: float) -> float:
    """Применить корректировку цены по показателю ERR."""
    if err_percent > 20:
        return price * 1.2
    if err_percent > 15:
        return price * 1.1
    return price


def calculate_recommended_price(
    avg_reach: int,
    category: str,
    err_percent: float = 0,
    format_type: str = "1/24",
    cpm_override: int = None,
    avg_reach_48h: int = 0
) -> int:
    """
    Рассчитать рекомендуемую цену размещения.
    """
    # Получаем CPM
    if cpm_override:
        base_cpm = cpm_override
    else:
        category_info = CHANNEL_CATEGORIES.get(category, {"cpm": 1000})
        base_cpm = category_info.get("cpm", 1000)
    
    # Базовая цена = (охват × CPM) / 1000
    base_price = (avg_reach * base_cpm) / 1000
    base_price = _apply_err_adjustment(base_price, err_percent)
    
    # Корректировка по формату:
    # 1/24 — базовая цена
    # 1/48 — цена по охвату 48ч (если доступен) или 1.5x от 1/24
    # 2/48 — два поста = 2x от 1/24
    # native — навсегда = 2.5x от 1/24
    if format_type == "1/48":
        if avg_reach_48h > 0:
            base_price = (avg_reach_48h * base_cpm) / 1000
            base_price = _apply_err_adjustment(base_price, err_percent)
        else:
            base_price *= 1.5
    elif format_type == "2/48":
        base_price *= 2.0
    elif format_type == "native":
        base_price *= 2.5
    
    return int(base_price)


def format_number(num: float) -> str:
    """Форматировать число с разделителями"""
    return f"{num:,.0f}".replace(",", " ")


def format_price(price: float) -> str:
    """Форматировать цену"""
    return f"{price:,.0f}₽".replace(",", " ")


def get_status_emoji(status: str) -> str:
    """Получить эмодзи статуса"""
    statuses = {
        "pending": "⏳",
        "payment_uploaded": "📤",
        "payment_confirmed": "✅",
        "posted": "📝",
        "completed": "✔️",
        "cancelled": "❌",
        "moderation": "🔍",
        "approved": "✅",
        "rejected": "❌"
    }
    return statuses.get(status, "❓")


def escape_md(text: str) -> str:
    """Escape special characters for Telegram's Markdown parse mode.

    Returns an empty string for None or empty input to prevent crashes when
    database fields are unexpectedly None.
    """
    if not text:
        return ""
    for char in ("_", "*", "`", "[", "]"):
        text = text.replace(char, f"\\{char}")
    return text


def truncate_text(text: str, max_length: int = 100) -> str:
    """Обрезать текст"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."


def channel_link(name: str, username: Optional[str]) -> str:
    """Вернуть Markdown-ссылку на канал вида [Название](https://t.me/username).

    Если username не задан (или равен '—'), возвращает просто название,
    экранируя специальные символы Markdown v1.
    """
    if not name:
        return "—"
    if not username or username in ("—", ""):
        return escape_md(name)
    safe_name = name.replace("]", "\\]")
    return f"[{safe_name}](https://t.me/{username})"


def format_channel_stats_for_group(channel, order_id: Optional[int] = None) -> str:
    """Форматировать карточку статистики канала для чата менеджеров.

    Формат:
        📣 [Название канала](https://t.me/username) 👥 6,991
        👁 24ч: 403 | 48ч: 545 | 72ч: 564
        📈 ER24: 6.22%
    """
    name = channel.name or "Канал"
    username = getattr(channel, "username", None)
    subscribers = channel.subscribers or 0
    reach_24h = channel.avg_reach_24h or 0
    reach_48h = channel.avg_reach_48h or 0
    reach_72h = channel.avg_reach_72h or 0
    err24 = float(channel.err24_percent or channel.err_percent or 0)

    text = f"📣 {channel_link(name, username)} 👥 {subscribers:,}\n"
    text += f"👁 24ч: {reach_24h:,} | 48ч: {reach_48h:,} | 72ч: {reach_72h:,}\n"
    text += f"📈 ER24: {err24:.2f}%"

    if order_id:
        text += f"\n\n💼 Заказ #{order_id}"

    return text


# Русские названия дней недели (именительный падеж)
_WEEKDAY_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

# Русские названия месяцев (родительный падеж)
_MONTH_RU_GEN = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _category_emoji(category: Optional[str]) -> str:
    """Вернуть эмодзи категории канала (первый символ из имени категории, если есть)."""
    from config import CHANNEL_CATEGORIES
    if not category:
        return "📢"
    cat = CHANNEL_CATEGORIES.get(category)
    if not cat:
        return "📢"
    name = cat.get("name", "")
    # Имя вида "🧠 Психология и отношения" — берём первый символ
    stripped = name.strip()
    if stripped:
        # emoji занимают > 1 байта, берём первый «символ» (codepoint)
        first = stripped[0]
        if ord(first) > 127:
            return first
    return "📢"


def format_daily_schedule(posts_data: list, target_date) -> str:
    """Форматировать расписание публикаций на день для чата менеджеров.

    Args:
        posts_data: список словарей с ключами:
            - channel_name (str)
            - channel_category (str | None)
            - scheduled_time (datetime)
            - price (float | None)
            - payment_method (str | None)
            - manager_name (str | None)
            - status (str)  — статус ScheduledPost
        target_date: объект date для заголовка

    Формат вывода:
        📅 6 апреля (Понедельник)

        🧘 Название канала
        🔅10:10 бронь 55р пдп (Даня)
        -
        -
        Комментарий:
    """
    day = target_date.day
    month = _MONTH_RU_GEN[target_date.month]
    weekday = _WEEKDAY_RU[target_date.weekday()]

    lines = [f"📅 {day} {month} ({weekday})"]

    if not posts_data:
        lines.append("\n_(нет запланированных публикаций)_")
        return "\n".join(lines)

    for entry in posts_data:
        channel_name = entry.get("channel_name") or "Канал"
        category = entry.get("channel_category")
        ch_emoji = _category_emoji(category)

        scheduled_dt = entry.get("scheduled_time")
        time_str = scheduled_dt.strftime("%H:%M") if scheduled_dt else "--:--"

        price = entry.get("price")
        price_str = f"{int(price)}р" if price else ""

        payment = entry.get("payment_method") or ""

        manager_name = entry.get("manager_name") or ""
        manager_part = f" ({manager_name})" if manager_name else ""

        # Собираем строку бронирования
        booking_parts = [time_str, "бронь"]
        if price_str:
            booking_parts.append(price_str)
        if payment:
            booking_parts.append(payment)

        booking_line = "🔅" + " ".join(booking_parts) + manager_part

        lines.append(f"\n{ch_emoji} {channel_name}")
        lines.append(booking_line)
        lines.append("-")
        lines.append("-")
        lines.append("Комментарий:")

    return "\n".join(lines)

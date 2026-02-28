"""
Вспомогательные функции
"""
import logging
from typing import Optional
from datetime import datetime

from aiogram import Bot

from config import CHANNEL_CATEGORIES


logger = logging.getLogger(__name__)


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


def calculate_recommended_price(
    avg_reach: int,
    category: str,
    err_percent: float = 0,
    format_type: str = "1/24",
    cpm_override: int = None
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
    
    # Корректировка по ERR
    if err_percent > 20:
        base_price *= 1.2
    elif err_percent > 15:
        base_price *= 1.1
    
    # Корректировка по формату
    format_multipliers = {
        "1/24": 1.0,
        "1/48": 0.8,
        "2/48": 1.6,
        "native": 2.5
    }
    base_price *= format_multipliers.get(format_type, 1.0)
    
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


def truncate_text(text: str, max_length: int = 100) -> str:
    """Обрезать текст"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."

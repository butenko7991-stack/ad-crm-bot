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
    
    # Корректировка по ERR
    if err_percent > 20:
        base_price *= 1.2
    elif err_percent > 15:
        base_price *= 1.1
    
    # Корректировка по формату:
    # 1/24 — базовая цена
    # 1/48 — цена по охвату 48ч (если доступен) или 1.5x от 1/24
    # 2/48 — два поста = 2x от 1/24
    # native — навсегда = 2.5x от 1/24
    if format_type == "1/48":
        if avg_reach_48h > 0:
            base_price = (avg_reach_48h * base_cpm) / 1000
            if err_percent > 20:
                base_price *= 1.2
            elif err_percent > 15:
                base_price *= 1.1
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


def truncate_text(text: str, max_length: int = 100) -> str:
    """Обрезать текст"""
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + "..."

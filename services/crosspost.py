"""
Сервис кросспостинга из Telegram в сеть Max.

Публикует содержимое запланированных постов в чат Max после
успешной публикации в Telegram-канале. Поддерживает лимит
количества кросспостов в сутки (настраивается через admin-панель).
"""
import logging
import traceback
from datetime import date
from typing import Optional

from sqlalchemy import func, select

from database import async_session_maker, ScheduledPost
from services.settings import (
    get_setting,
    CROSSPOST_ENABLED_KEY,
    CROSSPOST_DAILY_LIMIT_KEY,
    MAX_CROSSPOST_CHAT_ID_KEY,
)
from utils.helpers import utc_now

logger = logging.getLogger(__name__)

# Значение лимита по умолчанию (0 = без ограничений)
DEFAULT_DAILY_LIMIT = 10


async def is_crosspost_enabled() -> bool:
    """Проверить, включён ли кросспостинг в сеть Max глобально."""
    val = await get_setting(CROSSPOST_ENABLED_KEY, default="false")
    return (val or "").lower() in ("1", "true", "yes")


async def get_crosspost_daily_limit() -> int:
    """Получить лимит кросспостов в сутки (0 = без ограничений)."""
    val = await get_setting(CROSSPOST_DAILY_LIMIT_KEY)
    if val is not None:
        try:
            return max(0, int(val))
        except (ValueError, TypeError):
            pass
    return DEFAULT_DAILY_LIMIT


async def get_max_crosspost_chat_id() -> Optional[int]:
    """Получить ID чата Max для кросспостинга.

    Приоритет:
      1. Значение из bot_settings (установлено через admin-панель).
      2. Переменная окружения MAX_CROSSPOST_CHAT_ID.
    """
    from config import MAX_CROSSPOST_CHAT_ID as _env_val  # type: ignore[attr-defined]
    db_val = await get_setting(MAX_CROSSPOST_CHAT_ID_KEY)
    if db_val:
        try:
            return int(db_val)
        except (ValueError, TypeError):
            logger.warning(f"Некорректное значение max_crosspost_chat_id в БД: {db_val!r}")
    return _env_val


async def get_daily_crosspost_count() -> int:
    """Подсчитать количество кросспостов, выполненных сегодня (UTC).

    Граница суток определяется по UTC: с 00:00:00 до 23:59:59 текущей UTC-даты.
    Все временны́е метки в БД хранятся в UTC, поэтому подсчёт корректен.
    """
    today_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start.replace(hour=23, minute=59, second=59, microsecond=999999)
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(func.count(ScheduledPost.id)).where(
                    ScheduledPost.max_posted_at >= today_start,
                    ScheduledPost.max_posted_at <= today_end,
                )
            )
            return result.scalar_one() or 0
    except Exception as e:
        logger.error(f"Ошибка подсчёта кросспостов за сегодня: {e}")
        return 0


async def can_crosspost_today() -> bool:
    """Вернуть True, если дневной лимит кросспостов ещё не исчерпан."""
    limit = await get_crosspost_daily_limit()
    if limit == 0:
        return True  # 0 = без ограничений
    count = await get_daily_crosspost_count()
    return count < limit


async def crosspost_post_to_max(post: ScheduledPost, max_bot) -> bool:
    """Отправить пост в чат Max.

    Возвращает True при успехе, False при ошибке или если условия не выполнены.
    Обновляет поля max_post_id и max_posted_at в переданном объекте поста
    (изменения должны быть зафиксированы в БД вызывающим кодом).
    """
    if not await is_crosspost_enabled():
        return False

    if not await can_crosspost_today():
        logger.info(f"Дневной лимит кросспостов исчерпан — пост #{post.id} не будет скопирован в Max")
        return False

    chat_id = await get_max_crosspost_chat_id()
    if not chat_id:
        logger.warning("MAX_CROSSPOST_CHAT_ID не задан — кросспост невозможен")
        return False

    # Формируем текст для Max (без HTML-тегов, Max поддерживает только markdown)
    text = post.content or ""
    if post.signature:
        if " | " in post.signature:
            parts = post.signature.split(" | ", 1)
            sig_text = parts[0].strip()
            sig_url = parts[1].strip()
            if sig_text and sig_url.startswith(("http://", "https://")):
                text = f"{text}\n\n[{sig_text}]({sig_url})" if text else f"[{sig_text}]({sig_url})"
            else:
                text = f"{text}\n\n{post.signature}" if text else post.signature
        else:
            text = f"{text}\n\n{post.signature}" if text else post.signature

    if not text:
        # Пост состоит только из медиафайла без текста — пропускаем, Max не поддерживает Telegram file_id
        logger.info(f"Пост #{post.id} содержит только медиа без текста — кросспост пропущен")
        return False

    try:
        sent = await max_bot.send_message(chat_id=chat_id, text=text)
        raw_id = getattr(sent, "message_id", None) or getattr(sent, "id", None)
        if raw_id is None:
            logger.warning(f"Пост #{post.id}: Max API не вернул ID сообщения — запись без ID")
        post.max_post_id = str(raw_id) if raw_id is not None else "unknown"
        post.max_posted_at = utc_now()
        logger.info(f"Пост #{post.id} скопирован в Max (chat_id={chat_id})")
        return True
    except Exception:
        logger.error(f"Ошибка кросспостинга поста #{post.id} в Max: {traceback.format_exc()}")
        return False

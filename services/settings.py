"""
Сервис для хранения и получения настроек бота из БД.

Настройки хранятся в таблице bot_settings (key-value).
При чтении в первую очередь используется значение из БД;
если оно не задано, применяется значение из переменной окружения / config.py.
"""
import logging
from datetime import datetime
from typing import Optional

from database import async_session_maker, BotSetting
from utils.helpers import utc_now

logger = logging.getLogger(__name__)

# ─── Ключи настроек ───────────────────────────────────────────────────────────
MANAGER_GROUP_CHAT_ID_KEY = "manager_group_chat_id"
PAYMENT_LINK_KEY = "payment_link"
CROSSPOST_ENABLED_KEY = "crosspost_enabled"
CROSSPOST_DAILY_LIMIT_KEY = "crosspost_daily_limit"
MAX_CROSSPOST_CHAT_ID_KEY = "max_crosspost_chat_id"


async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Получить значение настройки по ключу."""
    try:
        async with async_session_maker() as session:
            row = await session.get(BotSetting, key)
            return row.value if row else default
    except Exception as e:
        logger.error(f"Ошибка чтения настройки '{key}': {e}")
        return default


async def set_setting(key: str, value: Optional[str], updated_by: Optional[int] = None) -> None:
    """Сохранить (или обновить) значение настройки."""
    try:
        async with async_session_maker() as session:
            row = await session.get(BotSetting, key)
            if row:
                row.value = value
                row.updated_at = utc_now()
                row.updated_by = updated_by
            else:
                session.add(BotSetting(
                    key=key,
                    value=value,
                    updated_at=utc_now(),
                    updated_by=updated_by,
                ))
            await session.commit()
    except Exception as e:
        logger.error(f"Ошибка сохранения настройки '{key}': {e}")


async def get_manager_group_chat_id() -> Optional[int]:
    """Вернуть ID чата менеджеров.

    Приоритет:
      1. Значение из таблицы bot_settings (установлено через UI).
      2. Переменная окружения MANAGER_GROUP_CHAT_ID (config.py).
    """
    from config import MANAGER_GROUP_CHAT_ID as _env_val
    db_val = await get_setting(MANAGER_GROUP_CHAT_ID_KEY)
    if db_val:
        try:
            return int(db_val)
        except (ValueError, TypeError):
            logger.warning(f"Некорректное значение manager_group_chat_id в БД: {db_val!r}")
    return _env_val

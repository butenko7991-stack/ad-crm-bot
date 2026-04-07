"""
Рассылка сервисных сообщений всем пользователям бота.

Используется для уведомлений об обновлениях после каждого деплоя.
"""
import asyncio
import logging
import traceback

from aiogram import Bot
from aiogram.enums import ParseMode
from sqlalchemy import select, union

from config import UPDATE_VERSION, UPDATE_NOTES
from database import async_session_maker, Client, Manager
from services.settings import get_setting, set_setting

logger = logging.getLogger(__name__)

# Ключ в bot_settings, хранящий версию последней отправленной рассылки
LAST_BROADCAST_VERSION_KEY = "last_broadcast_version"


async def _get_all_user_ids() -> list[int]:
    """Возвращает уникальный список telegram_id всех пользователей (клиенты + менеджеры)."""
    try:
        async with async_session_maker() as session:
            clients_q = select(Client.telegram_id).where(Client.telegram_id.isnot(None))
            managers_q = select(Manager.telegram_id).where(Manager.telegram_id.isnot(None))

            result = await session.execute(union(clients_q, managers_q))
            return [row[0] for row in result.fetchall()]
    except Exception:
        logger.error(f"Ошибка получения списка пользователей для рассылки: {traceback.format_exc()}")
        return []


async def send_update_broadcast(bot: Bot) -> None:
    """Проверяет, была ли рассылка для текущей версии бота, и если нет — отправляет её.

    Вызывается один раз при старте бота. Если ``UPDATE_VERSION`` из config.py
    совпадает с последней записанной в БД версией рассылки, ничего не происходит.
    """
    try:
        last_version = await get_setting(LAST_BROADCAST_VERSION_KEY)
        if last_version == UPDATE_VERSION:
            logger.info(
                f"Рассылка об обновлении уже была отправлена для версии {UPDATE_VERSION} — пропускаем"
            )
            return

        user_ids = await _get_all_user_ids()
        if not user_ids:
            logger.info("Нет пользователей для рассылки об обновлении")
            await set_setting(LAST_BROADCAST_VERSION_KEY, UPDATE_VERSION)
            return

        logger.info(f"Рассылка обновления v{UPDATE_VERSION} — {len(user_ids)} пользователей")

        sent = 0
        failed = 0
        for user_id in user_ids:
            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=UPDATE_NOTES,
                    parse_mode=ParseMode.MARKDOWN,
                )
                sent += 1
            except Exception:
                failed += 1
            # Небольшая задержка, чтобы не превышать лимиты Telegram (~10 msg/sec)
            await asyncio.sleep(0.1)

        logger.info(
            f"Рассылка обновления v{UPDATE_VERSION} завершена: "
            f"отправлено {sent}, ошибок {failed}"
        )
        await set_setting(LAST_BROADCAST_VERSION_KEY, UPDATE_VERSION)

    except Exception:
        logger.error(f"Ошибка рассылки обновления: {traceback.format_exc()}")

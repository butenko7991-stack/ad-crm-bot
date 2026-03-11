"""
CRM Bot для продажи рекламы в Telegram-каналах и сети Max
Точка входа
"""
import asyncio
import logging
import traceback
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import BOT_TOKEN, MAX_BOT_TOKEN, ADMIN_IDS
from database import init_db, async_session_maker
from database.models import Slot
from handlers import setup_routers


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def cleanup_expired_slots():
    """Освобождаем слоты, у которых истёк срок резервации"""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Slot).where(
                    Slot.status == "reserved",
                    Slot.reserved_until < datetime.utcnow()
                )
            )
            expired_slots = result.scalars().all()
            if expired_slots:
                for slot in expired_slots:
                    slot.status = "available"
                    slot.reserved_by = None
                    slot.reserved_until = None
                await session.commit()
                logger.info(f"Освобождено {len(expired_slots)} просроченных слотов")
    except Exception:
        logger.error(f"Ошибка очистки слотов: {traceback.format_exc()}")


async def global_error_handler(update: Update, exception: Exception) -> bool:
    """
    Глобальный обработчик ошибок.
    """
    # Логируем полную ошибку
    logger.error(f"Ошибка: {exception}\n{traceback.format_exc()}")
    return True


async def run_max_bot():
    """Запускает бота в сети Max (если MAX_BOT_TOKEN задан)."""
    if not MAX_BOT_TOKEN:
        logger.info("MAX_BOT_TOKEN не задан — бот в сети Max не запускается")
        return

    try:
        from maxapi import Bot as MaxBot
        from max_bot import setup_max_dispatcher
    except ImportError:
        logger.error("Библиотека maxapi не установлена. Установите её: pip install maxapi")
        return

    max_bot = MaxBot(MAX_BOT_TOKEN)
    max_dp = setup_max_dispatcher()

    logger.info("🚀 Max-бот запускается...")
    try:
        await max_dp.start_polling(max_bot)
    except Exception:
        logger.error(f"Ошибка Max-бота: {traceback.format_exc()}")


async def main():
    """Главная функция запуска бота"""
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен!")
        return
    
    # Инициализация бота
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )
    
    # Инициализация диспетчера
    dp = Dispatcher()
    
    # Подключаем роутеры
    main_router = setup_routers()
    dp.include_router(main_router)
    
    # Инициализация БД
    logger.info("Инициализация базы данных...")
    await init_db()
    
    # Планировщик задач
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        cleanup_expired_slots,
        trigger="interval",
        minutes=5,
        id="cleanup_expired_slots"
    )
    scheduler.start()
    logger.info("Планировщик задач запущен")
    
    # Запуск бота
    logger.info("🚀 Бот запускается...")
    
    # Уведомляем админов о запуске
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "✅ Бот запущен!")
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")
    
    try:
        # Запускаем Telegram-бот и Max-бот параллельно
        await asyncio.gather(
            dp.start_polling(bot),
            run_max_bot(),
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

"""
CRM Bot для продажи рекламы в Telegram-каналах
Точка входа
"""
import asyncio
import logging
import traceback
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update

from config import BOT_TOKEN, ADMIN_IDS
from database import init_db
from handlers import setup_routers


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def global_error_handler(update: Update, exception: Exception) -> bool:
    """
    Глобальный обработчик ошибок.
    """
    # Логируем полную ошибку
    logger.error(f"Ошибка: {exception}\n{traceback.format_exc()}")
    return True


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
    
    # Запуск бота
    logger.info("🚀 Бот запускается...")
    
    # Уведомляем админов о запуске
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, "✅ Бот запущен!")
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

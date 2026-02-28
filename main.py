"""
CRM Bot для продажи рекламы в Telegram-каналах
Точка входа
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN
from database import init_db
from handlers import setup_routers


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

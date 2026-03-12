"""
CRM Bot для продажи рекламы в Telegram-каналах и сети Max
Точка входа
"""
import asyncio
import logging
import traceback
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Update
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from config import BOT_TOKEN, MAX_BOT_TOKEN, ADMIN_IDS
from database import init_db, async_session_maker
from database.models import Slot, ScheduledPost, Channel
from handlers import setup_routers
from services.channel_collector import refresh_all_channels


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


async def publish_scheduled_posts(bot: Bot):
    """Публикуем посты, время которых наступило, и уведомляем админов"""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledPost).where(
                    ScheduledPost.status == "pending",
                    ScheduledPost.scheduled_time <= datetime.utcnow()
                )
            )
            posts = result.scalars().all()

            for post in posts:
                channel = await session.get(Channel, post.channel_id)
                if not channel:
                    logger.warning(f"Канал #{post.channel_id} не найден для поста #{post.id}")
                    post.status = "error"
                    await session.commit()
                    continue

                channel_tg_id = channel.telegram_id
                caption = post.content or None

                try:
                    if post.file_id and post.file_type == "photo":
                        sent = await bot.send_photo(
                            chat_id=channel_tg_id,
                            photo=post.file_id,
                            caption=caption,
                            parse_mode=None,
                        )
                    elif post.file_id and post.file_type == "video":
                        sent = await bot.send_video(
                            chat_id=channel_tg_id,
                            video=post.file_id,
                            caption=caption,
                            parse_mode=None,
                        )
                    elif post.file_id and post.file_type == "document":
                        sent = await bot.send_document(
                            chat_id=channel_tg_id,
                            document=post.file_id,
                            caption=caption,
                            parse_mode=None,
                        )
                    else:
                        sent = await bot.send_message(
                            chat_id=channel_tg_id,
                            text=post.content or "",
                            parse_mode=None,
                        )

                    post.status = "posted"
                    post.posted_at = datetime.utcnow()
                    post.message_id = sent.message_id
                    await session.commit()
                    logger.info(f"Пост #{post.id} опубликован в канале {channel.name} (msg_id={sent.message_id})")

                    # Уведомляем администраторов
                    ch_name = channel.name
                    content_len = len(post.content) if post.content else 0
                    text_preview = (post.content[:50] + "…") if content_len > 50 else (post.content or "📎 медиа")
                    notify_text = (
                        f"✅ Пост #{post.id} опубликован!\n\n"
                        f"📢 Канал: {ch_name}\n"
                        f"🕐 Время публикации: {post.posted_at.strftime('%d.%m.%Y %H:%M')} UTC\n"
                        f"📝 Превью: {text_preview}"
                    )
                    for admin_id in ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, notify_text, parse_mode=None)
                        except Exception:
                            logger.warning(f"Не удалось уведомить админа {admin_id} о посте #{post.id}", exc_info=True)

                except Exception:
                    logger.error(f"Ошибка публикации поста #{post.id}: {traceback.format_exc()}")
                    post.status = "error"
                    await session.commit()

    except Exception:
        logger.error(f"Ошибка в publish_scheduled_posts: {traceback.format_exc()}")


async def delete_posted_posts(bot: Bot):
    """Удаляем опубликованные посты из каналов по истечении delete_after_hours"""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledPost).where(
                    ScheduledPost.status == "posted",
                    ScheduledPost.delete_after_hours > 0,
                    ScheduledPost.posted_at.isnot(None),
                    ScheduledPost.deleted_at.is_(None),
                    ScheduledPost.message_id.isnot(None),
                )
            )
            posts = result.scalars().all()

            now = datetime.utcnow()
            for post in posts:
                if post.posted_at + timedelta(hours=post.delete_after_hours) > now:
                    continue

                channel = await session.get(Channel, post.channel_id)
                if not channel:
                    post.deleted_at = now
                    await session.commit()
                    continue

                try:
                    await bot.delete_message(
                        chat_id=channel.telegram_id,
                        message_id=post.message_id,
                    )
                except Exception:
                    logger.warning(f"Не удалось удалить сообщение поста #{post.id} из канала: {traceback.format_exc()}")

                post.deleted_at = now
                post.status = "deleted"
                await session.commit()
                logger.info(f"Пост #{post.id} удалён из канала {channel.name}")

    except Exception:
        logger.error(f"Ошибка в delete_posted_posts: {traceback.format_exc()}")


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
    scheduler.add_job(
        publish_scheduled_posts,
        trigger="interval",
        minutes=1,
        id="publish_scheduled_posts",
        args=[bot],
    )
    scheduler.add_job(
        delete_posted_posts,
        trigger="interval",
        minutes=15,
        id="delete_posted_posts",
        args=[bot],
    )
    scheduler.add_job(
        refresh_all_channels,
        trigger="interval",
        hours=6,
        id="refresh_all_channels",
        args=[bot],
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

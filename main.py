"""
CRM Bot для продажи рекламы в Telegram-каналах и сети Max
Точка входа
"""
import asyncio
import html as html_module
import json
import logging
import traceback
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ErrorEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update as sa_update

from config import BOT_TOKEN, MAX_BOT_TOKEN, ADMIN_IDS, ADMIN_PASSWORD, LOCAL_TZ_OFFSET
from database import init_db, async_session_maker
from database.models import Slot, ScheduledPost, Channel, PostAnalytics
from handlers import setup_routers
from services.channel_collector import refresh_all_channels
from services.settings import get_manager_group_chat_id
from services.crosspost import crosspost_post_to_max
from services.error_library import lookup_error, record_unknown_error
from utils.helpers import format_channel_stats_for_group, utc_now


# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Блокировка, предотвращающая повторный вход в publish_scheduled_posts
# внутри одного процесса (дополнительная защита к max_instances=1 в планировщике).
_publishing_lock = asyncio.Lock()

# Глобальный экземпляр Max-бота (устанавливается в run_max_bot() при старте).
# Используется для кросспостинга постов из Telegram в Max.
_max_bot_instance = None

async def cleanup_expired_slots():
    """Освобождаем слоты, у которых истёк срок резервации"""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Slot).where(
                    Slot.status == "reserved",
                    Slot.reserved_until < utc_now()
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


async def _reset_stale_publishing_posts():
    """Переводим «застрявшие» посты из publishing → error при старте бота.

    Такие посты возникают, если предыдущий запуск завершился аварийно между
    отправкой сообщения в Telegram и фиксацией статуса «опубликован» в БД.
    Поскольку мы не знаем, был ли пост в итоге отправлен, мы выбираем
    стратегию «не публиковать дважды»: помечаем такие посты как ошибочные
    и оставляем решение о повторной публикации администратору.
    """
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                sa_update(ScheduledPost)
                .where(ScheduledPost.status == "publishing")
                .values(status="error")
                .returning(ScheduledPost.id)
            )
            await session.commit()
            stale_ids = [row[0] for row in result.fetchall()]
            if stale_ids:
                logger.warning(
                    f"Найдено {len(stale_ids)} постов в статусе 'publishing' "
                    f"(ID: {stale_ids}) — переведены в 'error'. "
                    f"Предыдущий запуск бота, вероятно, завершился в процессе "
                    f"публикации. При необходимости перезапустите посты вручную."
                )
    except Exception:
        logger.error(f"Ошибка сброса застрявших постов: {traceback.format_exc()}")


async def publish_scheduled_posts(bot: Bot):
    """Публикуем посты, время которых наступило, и уведомляем админов"""
    # Быстрая проверка без захвата блокировки — если уже выполняется, просто выходим
    if _publishing_lock.locked():
        logger.info("publish_scheduled_posts уже выполняется — пропускаем этот запуск")
        return
    async with _publishing_lock:
        await _do_publish_scheduled_posts(bot)


async def _do_publish_scheduled_posts(bot: Bot):
    """Внутренняя реализация публикации постов (вызывается под блокировкой)."""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledPost).where(
                    ScheduledPost.status == "pending",
                    ScheduledPost.scheduled_time <= utc_now()
                )
            )
            posts = result.scalars().all()

            for post in posts:
                # Атомарно «захватываем» пост: меняем статус pending → publishing.
                # Если другой процесс или предыдущий запуск уже изменил статус,
                # RETURNING вернёт пустой результат — пропускаем этот пост.
                claim = await session.execute(
                    sa_update(ScheduledPost)
                    .where(
                        ScheduledPost.id == post.id,
                        ScheduledPost.status == "pending",
                    )
                    .values(status="publishing")
                    .returning(ScheduledPost.id)
                )
                await session.commit()
                if claim.scalar_one_or_none() is None:
                    logger.info(f"Пост #{post.id} уже обрабатывается другим процессом — пропускаем")
                    continue

                channel = await session.get(Channel, post.channel_id)
                if not channel:
                    logger.warning(f"Канал #{post.channel_id} не найден для поста #{post.id}")
                    post.status = "error"
                    await session.commit()
                    continue

                channel_tg_id = channel.telegram_id

                # Строим текст/подпись поста с учётом подписи со скрытой ссылкой
                post_parse_mode = None
                if post.signature:
                    # Экранируем основной контент для HTML-режима
                    escaped_content = html_module.escape(post.content or "")
                    # Формат «Текст | URL» — кликабельная подпись с произвольной ссылкой
                    if " | " in post.signature:
                        parts = post.signature.split(" | ", 1)
                        sig_text = parts[0].strip()
                        sig_url = parts[1].strip()
                        if sig_text and sig_url.startswith(("http://", "https://", "tg://")):
                            sig_html = (
                                f'<a href="{html_module.escape(sig_url)}">'
                                f'{html_module.escape(sig_text)}</a>'
                            )
                        else:
                            sig_html = html_module.escape(post.signature)
                    else:
                        channel_username = channel.username if channel.username else None
                        channel_url = f"https://t.me/{channel_username}" if channel_username else None
                        if channel_url:
                            sig_html = (
                                f'<a href="{html_module.escape(channel_url)}">'
                                f'{html_module.escape(post.signature)}</a>'
                            )
                        else:
                            sig_html = html_module.escape(post.signature)
                    post_text = f"{escaped_content}\n\n{sig_html}" if escaped_content else sig_html
                    post_parse_mode = "HTML"
                else:
                    post_text = post.content or ""
                caption = post_text or None

                # Build inline keyboard from saved buttons (if any)
                post_markup = None
                if post.inline_buttons:
                    try:
                        btns = json.loads(post.inline_buttons)
                        if btns:
                            post_markup = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text=b["text"], url=b["url"])]
                                for b in btns
                            ])
                    except Exception:
                        logger.warning(
                            f"Не удалось разобрать inline_buttons поста #{post.id} — "
                            f"пост будет опубликован без кнопок",
                            exc_info=True,
                        )

                # Отправляем пост в канал — только ошибки отправки меняют статус
                sent = None
                try:
                    if post.file_id and post.file_type == "photo":
                        sent = await bot.send_photo(
                            chat_id=channel_tg_id,
                            photo=post.file_id,
                            caption=caption,
                            parse_mode=post_parse_mode,
                            reply_markup=post_markup,
                        )
                    elif post.file_id and post.file_type == "video":
                        sent = await bot.send_video(
                            chat_id=channel_tg_id,
                            video=post.file_id,
                            caption=caption,
                            parse_mode=post_parse_mode,
                            reply_markup=post_markup,
                        )
                    elif post.file_id and post.file_type == "document":
                        sent = await bot.send_document(
                            chat_id=channel_tg_id,
                            document=post.file_id,
                            caption=caption,
                            parse_mode=post_parse_mode,
                            reply_markup=post_markup,
                        )
                    else:
                        sent = await bot.send_message(
                            chat_id=channel_tg_id,
                            text=post_text,
                            parse_mode=post_parse_mode,
                            reply_markup=post_markup,
                        )
                except Exception:
                    logger.error(f"Ошибка публикации поста #{post.id}: {traceback.format_exc()}")
                    post.status = "error"
                    await session.commit()
                    continue

                if sent is None:
                    logger.error(f"Пост #{post.id}: sent=None после отправки — пропускаем")
                    post.status = "error"
                    await session.commit()
                    continue

                # Пост отправлен — фиксируем статус «опубликован» немедленно.
                # Пост был атомарно захвачен в «publishing» перед отправкой,
                # поэтому повторная публикация другим экземпляром невозможна.
                posted_at = utc_now()
                post.status = "posted"
                post.posted_at = posted_at
                post.message_id = sent.message_id

                # Кросспостинг в Max (если включён для этого поста).
                # Примечание: _max_bot_instance проверяется на наличие, но не на
                # работоспособность соединения. При сбое Max-бота ошибка будет
                # поймана блоком except ниже и залогирована без прерывания основного потока.
                if post.crosspost_to_max and _max_bot_instance is not None:
                    try:
                        await crosspost_post_to_max(post, _max_bot_instance)
                    except Exception:
                        logger.warning(
                            f"Ошибка кросспостинга поста #{post.id} в Max — "
                            f"Telegram-пост уже опубликован",
                            exc_info=True,
                        )

                await session.commit()
                logger.info(f"Пост #{post.id} опубликован в канале {channel.name} (msg_id={sent.message_id})")

                # Создаём начальную запись аналитики в отдельной сессии,
                # чтобы любой сбой при её создании не переводил основную сессию
                # в состояние «нужен откат» и не прерывал обработку следующих
                # постов в той же очереди.
                try:
                    async with async_session_maker() as analytics_session:
                        existing_analytics = (await analytics_session.execute(
                            select(PostAnalytics).where(PostAnalytics.scheduled_post_id == post.id)
                        )).scalar_one_or_none()
                        if not existing_analytics:
                            analytics_session.add(PostAnalytics(
                                scheduled_post_id=post.id,
                                order_id=post.order_id,
                                channel_id=post.channel_id,
                            ))
                            await analytics_session.commit()
                except Exception:
                    logger.warning(
                        f"Не удалось создать запись аналитики для поста #{post.id} — "
                        f"статус поста уже зафиксирован как «опубликован»",
                        exc_info=True,
                    )

                # Уведомляем администраторов (сбои уведомлений не влияют на статус поста)
                ch_name = channel.name
                content_len = len(post.content) if post.content else 0
                text_preview = (post.content[:50] + "…") if content_len > 50 else (post.content or "📎 медиа")
                notify_text = (
                    f"✅ Пост #{post.id} опубликован!\n\n"
                    f"📢 Канал: {ch_name}\n"
                    f"🕐 Время публикации: {posted_at.strftime('%d.%m.%Y %H:%M')} UTC\n"
                    f"📝 Превью: {text_preview}"
                )
                for admin_id in ADMIN_IDS:
                    try:
                        await bot.send_message(admin_id, notify_text, parse_mode=None)
                    except Exception:
                        logger.warning(f"Не удалось уведомить админа {admin_id} о посте #{post.id}", exc_info=True)

                # Отправляем статистику канала и пересылаем пост в чат менеджеров
                # Посты с delete_after_hours=0 ("не удалять") не являются рекламными
                # и не отправляются в чат менеджеров
                mgr_chat_id = await get_manager_group_chat_id()
                if mgr_chat_id and post.delete_after_hours != 0:
                    try:
                        await bot.send_message(
                            mgr_chat_id,
                            format_channel_stats_for_group(channel),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        logger.warning(
                            f"Не удалось отправить статистику канала в чат менеджеров "
                            f"(пост #{post.id})",
                            exc_info=True,
                        )
                    try:
                        await bot.forward_message(
                            chat_id=mgr_chat_id,
                            from_chat_id=channel_tg_id,
                            message_id=sent.message_id,
                        )
                    except Exception:
                        logger.warning(
                            f"Не удалось переслать пост #{post.id} в чат менеджеров",
                            exc_info=True,
                        )

    except Exception:
        logger.error(f"Ошибка в _do_publish_scheduled_posts: {traceback.format_exc()}")


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

            now = utc_now()
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


async def send_daily_reach_report(bot: Bot):
    """Ежедневный отчёт об охватах рекламных постов за последние 24 часа.

    Отправляется всем администраторам и в чат менеджеров (если настроен).
    """
    try:
        from services.metrics import get_daily_reach_report, format_daily_reach_report_text

        data = await get_daily_reach_report()
        if data is None:
            return

        date_str = utc_now().strftime("%d.%m.%Y")
        text = format_daily_reach_report_text(data, date_str, bold="*")

        mgr_chat_id = await get_manager_group_chat_id()
        if mgr_chat_id:
            try:
                await bot.send_message(mgr_chat_id, text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                logger.warning("Не удалось отправить отчёт об охватах в чат менеджеров", exc_info=True)

        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                logger.warning(f"Не удалось отправить отчёт об охватах админу {admin_id}", exc_info=True)

        logger.info(f"Отчёт об охватах за сутки отправлен: {data['count']} постов")
    except Exception:
        logger.error(f"Ошибка send_daily_reach_report: {traceback.format_exc()}")


async def global_error_handler(event: ErrorEvent, bot: Bot) -> bool:
    """
    Глобальный обработчик ошибок aiogram 3.x.
    Логирует ошибку, обогащает уведомление подсказкой из библиотеки ошибок
    и отправляет уведомление всем администраторам.
    """
    exception = event.exception
    update = event.update

    # Полный трейсбэк в лог
    tb = traceback.format_exc()
    logger.error(
        f"Необработанное исключение при обработке обновления {update.update_id}: "
        f"{type(exception).__name__}: {exception}\n{tb}"
    )

    # Определяем контекст обновления для диагностики
    ctx_parts = []
    if update.message:
        ctx_parts.append(f"📩 Сообщение от user_id={update.message.from_user.id if update.message.from_user else '?'}")
        if update.message.text:
            ctx_parts.append(f"Текст: {update.message.text[:80]}")
    elif update.callback_query:
        ctx_parts.append(f"🔘 Callback от user_id={update.callback_query.from_user.id}")
        ctx_parts.append(f"Data: {update.callback_query.data}")
    ctx = "\n".join(ctx_parts) if ctx_parts else "нет деталей"

    # Трейсбэк (первые 600 символов, чтобы не флудить)
    tb_short = tb[-600:] if len(tb) > 600 else tb

    # Поиск в библиотеке ошибок
    known = lookup_error(exception, tb)
    if known:
        solution_block = (
            f"\n\n📖 *Известная ошибка:* {known['title']}\n"
            f"✅ *Решение:* {known['solution'][:400]}"
        )
    else:
        # Записываем неизвестную ошибку для пополнения библиотеки
        record_unknown_error(exception, tb, context=ctx)
        solution_block = "\n\n❓ *Ошибка не найдена в библиотеке* — записана для анализа."

    notify_text = (
        f"🚨 *Ошибка в боте*\n\n"
        f"*Тип:* `{type(exception).__name__}`\n"
        f"*Сообщение:* `{str(exception)[:200]}`\n\n"
        f"*Контекст:*\n{ctx}\n\n"
        f"*Трейсбэк (конец):*\n```\n{tb_short}\n```"
        f"{solution_block}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, notify_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logger.warning(f"Не удалось отправить уведомление об ошибке админу {admin_id}", exc_info=True)

    return True


async def run_max_bot():
    """Запускает бота в сети Max (если MAX_BOT_TOKEN задан)."""
    global _max_bot_instance

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
    _max_bot_instance = max_bot
    max_dp = setup_max_dispatcher()

    logger.info("🚀 Max-бот запускается...")
    try:
        await max_dp.start_polling(max_bot)
    except Exception:
        logger.error(f"Ошибка Max-бота: {traceback.format_exc()}")
    finally:
        _max_bot_instance = None


async def main():
    """Главная функция запуска бота"""
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен!")
        return

    # Проверяем небезопасный пароль по умолчанию
    if ADMIN_PASSWORD == "admin123":
        logger.warning(
            "⚠️  ADMIN_PASSWORD использует небезопасное значение по умолчанию 'admin123'. "
            "Установите надёжный пароль через переменную окружения ADMIN_PASSWORD."
        )
    
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

    # Регистрируем глобальный обработчик ошибок
    dp.errors.register(global_error_handler)
    
    # Инициализация БД
    logger.info("Инициализация базы данных...")
    await init_db()

    # Сброс «застрявших» постов из предыдущего запуска
    await _reset_stale_publishing_posts()

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
        max_instances=1,
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
    # Ежедневный отчёт об охватах: в 9:00 по местному времени (LOCAL_TZ_OFFSET)
    report_hour_utc = (9 - int(LOCAL_TZ_OFFSET.total_seconds() // 3600)) % 24
    scheduler.add_job(
        send_daily_reach_report,
        trigger="cron",
        hour=report_hour_utc,
        minute=0,
        id="daily_reach_report",
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

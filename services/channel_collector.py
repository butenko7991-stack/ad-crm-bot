"""
Сбор метрик каналов напрямую через Telegram Bot API.

Бот является администратором каналов и может:
 - Получать количество подписчиков через get_chat_member_count()
 - Принимать обновления channel_post / edited_channel_post, содержащие
   актуальное поле views для каждого поста
 - Пересчитывать avg_reach и ERR на основе накопленных PostAnalytics
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from sqlalchemy import select, func

from database import async_session_maker, Channel, PostAnalytics
from database.models import ScheduledPost

logger = logging.getLogger(__name__)


async def refresh_channel_subscribers(bot: Bot, channel: Channel) -> Optional[int]:
    """
    Обновить количество подписчиков канала через Bot API.

    Возвращает актуальное число подписчиков или None при ошибке.
    """
    try:
        member_count = await bot.get_chat_member_count(channel.telegram_id)
        async with async_session_maker() as session:
            ch = await session.get(Channel, channel.id)
            if ch:
                ch.subscribers = member_count
                ch.analytics_updated = datetime.now(timezone.utc).replace(tzinfo=None)
                await session.commit()
        logger.info(f"Канал «{channel.name}»: обновлено подписчиков → {member_count:,}")
        return member_count
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        logger.warning(f"Канал «{channel.name}»: нет доступа — {e}")
        return None
    except Exception as e:
        logger.error(f"Ошибка обновления подписчиков канала {channel.id}: {e}")
        return None


async def update_channel_reach_from_analytics(channel_id: int) -> dict:
    """
    Пересчитать avg_reach и ERR канала из накопленных данных PostAnalytics.

    Метод не требует сторонних сервисов: использует просмотры постов,
    которые бот автоматически фиксирует при получении channel_post /
    edited_channel_post обновлений.

    Возвращает словарь с обновлёнными показателями:
      avg_reach, err_percent, records_used
    """
    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                return {}

            # Берём последние 20 записей с ненулевыми просмотрами
            rows = (await session.execute(
                select(PostAnalytics.views, PostAnalytics.reactions,
                       PostAnalytics.forwards, PostAnalytics.saves,
                       PostAnalytics.comments)
                .where(
                    PostAnalytics.channel_id == channel_id,
                    PostAnalytics.views > 0,
                )
                .order_by(PostAnalytics.recorded_at.desc())
                .limit(20)
            )).all()

            if not rows:
                return {"records_used": 0}

            avg_views = sum(r.views for r in rows) / len(rows)
            avg_engage = sum(
                r.reactions + r.forwards + r.saves + r.comments for r in rows
            ) / len(rows)
            err = round(avg_engage / avg_views * 100, 2) if avg_views > 0 else 0
            err24 = err  # используем как ERR24 (нет разбивки по периодам)

            # Обновляем канал
            channel.avg_reach = int(avg_views)
            channel.avg_reach_24h = int(avg_views)
            channel.err_percent = err
            channel.err24_percent = err24
            channel.analytics_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()

            logger.info(
                f"Канал {channel_id}: avg_reach={int(avg_views)}, ERR={err}% "
                f"(по {len(rows)} постам)"
            )
            return {
                "avg_reach": int(avg_views),
                "err_percent": err,
                "records_used": len(rows),
            }
    except Exception as e:
        logger.error(f"Ошибка пересчёта reach/ERR для канала {channel_id}: {e}")
        return {}


async def refresh_channel_from_telemetr(channel: Channel) -> Optional[dict]:
    """
    Обновить аналитику канала через Telemetr API.

    Сохраняет в БД:
      avg_reach_24h, avg_reach_48h, avg_reach (из telemetr avg_views),
      err_percent, err24_percent, telemetr_id, subscribers.

    Возвращает словарь со свежими показателями, либо None если Telemetr
    не настроен или канал не найден в базе Telemetr.
    """
    from services.telemetr import telemetr_service

    if not telemetr_service.api_token:
        return None

    try:
        stats = await telemetr_service.get_full_stats(
            telegram_id=channel.telegram_id,
            username=channel.username,
        )
        if not stats:
            logger.info(f"Канал «{channel.name}»: не найден в Telemetr")
            return None

        async with async_session_maker() as session:
            ch = await session.get(Channel, channel.id)
            if not ch:
                return None

            if stats.get("internal_id"):
                ch.telemetr_id = str(stats["internal_id"])
            if stats.get("subscribers"):
                ch.subscribers = int(stats["subscribers"])
            avg_24h = int(stats.get("avg_views_24h") or 0)
            avg_48h = int(stats.get("avg_views_48h") or 0)
            avg_all = int(stats.get("avg_views") or 0)
            if avg_24h:
                ch.avg_reach_24h = avg_24h
            if avg_48h:
                ch.avg_reach_48h = avg_48h
            if avg_all:
                ch.avg_reach = avg_all
            err = float(stats.get("err_percent") or 0)
            err24 = float(stats.get("err24_percent") or 0)
            if err:
                ch.err_percent = err
            if err24:
                ch.err24_percent = err24
            ch.analytics_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.commit()

        logger.info(
            f"Канал «{channel.name}»: Telemetr — охват 24ч {avg_24h:,}, "
            f"ERR {err:.1f}%, id={stats.get('internal_id')}"
        )
        return stats
    except Exception as e:
        logger.error(f"Ошибка обновления канала {channel.id} через Telemetr: {e}")
        return None


async def record_post_views(
    channel_tg_id: int,
    message_id: int,
    views: int,
    reactions: int = 0,
    forwards: int = 0,
) -> bool:
    """
    Сохранить/обновить просмотры поста, полученные из channel_post обновления.

    Ищет ScheduledPost по (channel_tg_id, message_id), затем создаёт
    или обновляет запись PostAnalytics.

    Возвращает True, если запись была сохранена.
    """
    try:
        async with async_session_maker() as session:
            # Найти ScheduledPost
            channel_row = (await session.execute(
                select(Channel).where(Channel.telegram_id == channel_tg_id)
            )).scalar_one_or_none()

            if not channel_row:
                return False

            post = (await session.execute(
                select(ScheduledPost).where(
                    ScheduledPost.channel_id == channel_row.id,
                    ScheduledPost.message_id == message_id,
                    ScheduledPost.status.in_(["posted", "deleted"]),
                )
            )).scalar_one_or_none()

            if not post:
                return False

            # Найти или создать запись PostAnalytics
            analytics = (await session.execute(
                select(PostAnalytics).where(
                    PostAnalytics.scheduled_post_id == post.id
                )
            )).scalar_one_or_none()

            if analytics:
                # Обновляем только если новое значение больше
                if views > analytics.views:
                    analytics.views = views
                    analytics.reactions = max(reactions, analytics.reactions)
                    analytics.forwards = max(forwards, analytics.forwards)
                    analytics.recorded_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                analytics = PostAnalytics(
                    scheduled_post_id=post.id,
                    order_id=post.order_id,
                    channel_id=channel_row.id,
                    views=views,
                    reactions=reactions,
                    forwards=forwards,
                    recorded_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                session.add(analytics)

            await session.commit()
            logger.debug(
                f"Просмотры поста #{post.id} (msg {message_id}): {views}"
            )
            return True
    except Exception as e:
        logger.error(f"Ошибка record_post_views (msg={message_id}): {e}")
        return False


async def refresh_all_channels(bot: Bot) -> int:
    """
    Обновить подписчиков и пересчитать ERR/охват для всех активных каналов.

    Используется планировщиком для периодического обновления.
    Возвращает количество успешно обновлённых каналов.
    """
    updated = 0
    try:
        async with async_session_maker() as session:
            channels = (await session.execute(
                select(Channel).where(Channel.is_active == True)
            )).scalars().all()

        for channel in channels:
            count = await refresh_channel_subscribers(bot, channel)
            if count is not None:
                updated += 1
            # Пересчитываем avg_reach и ERR из накопленных PostAnalytics
            await update_channel_reach_from_analytics(channel.id)

        logger.info(f"Плановое обновление каналов: {updated}/{len(channels)} обновлено")
    except Exception as e:
        logger.error(f"Ошибка refresh_all_channels: {e}")
    return updated

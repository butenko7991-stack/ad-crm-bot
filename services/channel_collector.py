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

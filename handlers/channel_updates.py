"""
Обработчик обновлений канальных постов.

Когда бот является администратором канала, Telegram присылает обновления
типа channel_post (новый пост) и edited_channel_post (редактирование,
включая обновление счётчика просмотров).

Поле Message.views в этих обновлениях содержит актуальное число просмотров —
именно так бот собирает метрики без сторонних сервисов.
"""
import logging

from aiogram import Router
from aiogram.types import Message

from services.channel_collector import record_post_views

logger = logging.getLogger(__name__)
router = Router()


@router.channel_post()
async def on_channel_post(message: Message) -> None:
    """Фиксируем начальные просмотры при публикации поста."""
    if getattr(message, "views", None) is None:
        return
    await _save_views(message)


@router.edited_channel_post()
async def on_edited_channel_post(message: Message) -> None:
    """
    Telegram периодически присылает edited_channel_post по мере роста
    просмотров — каждый такой апдейт несёт свежее значение views.
    """
    if getattr(message, "views", None) is None:
        return
    await _save_views(message)


async def _save_views(message: Message) -> None:
    """Извлечь просмотры/реакции из сообщения и передать коллектору."""
    channel_tg_id = message.chat.id
    message_id = message.message_id
    views = getattr(message, "views", None) or 0

    # Суммарные реакции (если поддерживаются версией Bot API)
    reactions = 0
    if message.reactions:
        reactions = sum(r.count for r in message.reactions.reactions)

    # forward_count — число пересылок (появляется при edited_channel_post)
    forwards = getattr(message, "forward_count", 0) or 0

    saved = await record_post_views(
        channel_tg_id=channel_tg_id,
        message_id=message_id,
        views=views,
        reactions=reactions,
        forwards=forwards,
    )
    if saved:
        logger.debug(
            f"Зафиксированы просмотры: канал {channel_tg_id}, "
            f"msg {message_id}, views={views}"
        )

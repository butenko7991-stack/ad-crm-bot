"""
Обработчики для администратора
"""
import logging
import traceback
from datetime import datetime, date as date_type, time as time_type, timedelta, timezone
from decimal import Decimal

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, func

from config import ADMIN_IDS, ADMIN_PASSWORD, CHANNEL_CATEGORIES, AUTOPOST_ENABLED, CLAUDE_API_KEY, TELEMETR_API_TOKEN, MAX_BOT_TOKEN, MANAGER_LEVELS, MANAGER_GROUP_CHAT_ID, LOCAL_TZ_OFFSET, LOCAL_TZ_LABEL
from database import async_session_maker, Channel, Manager, Order, ScheduledPost, Competition, Slot, Client, CategoryCPM, PostAnalytics, PromoCode
from keyboards import get_admin_panel_menu, get_channel_settings_keyboard, get_category_keyboard
from keyboards.menus import get_cpm_categories_keyboard, get_autoposting_menu, get_post_analytics_keyboard, get_post_analytics_actions_keyboard, get_free_calendar_keyboard, get_time_picker_keyboard
from utils import AdminChannelStates, AdminPasswordState, AdminCompetitionStates, AdminPromoStates, format_channel_stats_for_group, AdminSettingsStates
from utils.states import AdminCPMStates, AdminAutopostingStates, AdminCreatePostStates, AdminSlotStates, AdminManagerStates
from services import gamification_service, get_manager_group_chat_id, set_setting, MANAGER_GROUP_CHAT_ID_KEY
from services.ai_trainer import ai_trainer_service
from services.diagnostics import run_diagnostics, run_deep_diagnostics, gather_business_metrics, get_improvement_suggestions


logger = logging.getLogger(__name__)
router = Router()

# Хранилище авторизованных админов
authenticated_admins = set()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def _md_escape(text: str) -> str:
    """Экранировать специальные символы Markdown v1 в пользовательских данных.

    В Telegram Markdown v1 специальны только: _ * ` [
    Каждый из них предваряется обратным слэшем.
    """
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


async def safe_edit_message(message, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    """Безопасное редактирование сообщения с обработкой ошибок.

    Если редактирование невозможно (сообщение не найдено, нельзя редактировать),
    отправляет новое сообщение вместо исключения.
    """
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        err = str(e)
        if "message is not modified" in err:
            pass  # Игнорируем — сообщение не изменилось
        elif any(s in err for s in ("message to edit not found", "MESSAGE_ID_INVALID",
                                    "message can't be edited", "bot was blocked",
                                    "chat not found")):
            # Нельзя отредактировать — отправляем новым сообщением
            await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            raise


async def get_channel_card(channel_id: int) -> tuple:
    """Получить данные канала и сформировать текст карточки"""
    async with async_session_maker() as session:
        channel = await session.get(Channel, channel_id)
        
        if not channel:
            return None, None, None
        
        ch_data = {
            "name": channel.name,
            "username": channel.username or "—",
            "subscribers": channel.subscribers or 0,
            "avg_reach": channel.avg_reach_24h or channel.avg_reach or 0,
            "category": channel.category,
            "is_active": channel.is_active,
            "prices": channel.prices or {},
            "cpm": float(channel.cpm or 0)
        }
    
    category_info = CHANNEL_CATEGORIES.get(ch_data["category"], {"name": "📁 Другое"})
    status = "✅ Активен" if ch_data["is_active"] else "❌ Неактивен"
    
    text = (
        f"⚙️ **Настройки канала**\n\n"
        f"📢 **{ch_data['name']}**\n"
        f"👤 @{ch_data['username']}\n"
        f"{category_info['name']}\n"
        f"{status}\n\n"
        f"👥 Подписчиков: **{ch_data['subscribers']:,}**\n"
        f"👁 Охват 24ч: **{ch_data['avg_reach']:,}**\n"
        f"💰 CPM: **{ch_data['cpm']:,.0f}₽**\n\n"
        f"**Цены:**\n"
        f"• 1/24: {ch_data['prices'].get('1/24', 0):,}₽\n"
        f"• 1/48: {ch_data['prices'].get('1/48', 0):,}₽\n"
        f"• 2/48: {ch_data['prices'].get('2/48', 0):,}₽\n"
        f"• Навсегда: {ch_data['prices'].get('native', 0):,}₽"
    )
    
    return text, ch_data["is_active"], channel_id


async def _notify_manager_group(bot: Bot, channel, order_id: int = None):
    """Отправить карточку статистики канала в чат менеджеров (если настроен)."""
    chat_id = await get_manager_group_chat_id()
    if not chat_id:
        return
    try:
        text = format_channel_stats_for_group(channel, order_id)
        await bot.send_message(chat_id, text, parse_mode=None)
    except Exception:
        logger.warning(
            f"Не удалось отправить статистику канала в чат менеджеров "
            f"(order_id={order_id}): {traceback.format_exc()}"
        )


# ==================== АВТОРИЗАЦИЯ ====================

@router.callback_query(F.data == "request_admin_password")
async def request_admin_password(callback: CallbackQuery, state: FSMContext):
    """Запросить пароль админа"""
    await callback.answer()
    await callback.message.answer("🔐 Введите пароль администратора:")
    await state.set_state(AdminPasswordState.waiting_admin_password)


@router.message(AdminPasswordState.waiting_admin_password)
async def check_admin_password(message: Message, state: FSMContext):
    """Проверить пароль админа"""
    try:
        await message.delete()
    except:
        pass
    
    if message.text == ADMIN_PASSWORD:
        authenticated_admins.add(message.from_user.id)
        await state.clear()
        await message.answer(
            "✅ **Добро пожаловать в админ-панель!**",
            reply_markup=get_admin_panel_menu(),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.answer("❌ Неверный пароль")
        await state.clear()


@router.callback_query(F.data == "admin_logout")
async def admin_logout(callback: CallbackQuery):
    """Выход из админки"""
    authenticated_admins.discard(callback.from_user.id)
    await callback.answer("👋 Вы вышли из админ-панели", show_alert=True)
    try:
        await callback.message.delete()
    except:
        pass


# ==================== АДМИН-ПАНЕЛЬ ====================

@router.callback_query(F.data == "adm_back")
async def adm_back(callback: CallbackQuery):
    """Назад в админ-панель"""
    await callback.answer()
    await safe_edit_message(
        callback.message,
        "⚙️ **Админ-панель**\n\nВыберите действие:",
        reply_markup=get_admin_panel_menu()
    )


# ==================== КАНАЛЫ ====================

@router.callback_query(F.data == "adm_channels")
async def adm_channels(callback: CallbackQuery):
    """Список каналов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(Channel))
            channels = result.scalars().all()
            channels_data = [{"id": ch.id, "name": ch.name, "is_active": ch.is_active} for ch in channels]
        
        if channels_data:
            text = "📢 **Каналы:**\n\n"
            buttons = []
            for ch in channels_data:
                status = "✅" if ch["is_active"] else "❌"
                text += f"{status} **{ch['name']}** (ID: {ch['id']})\n"
                buttons.append([InlineKeyboardButton(text=f"⚙️ {ch['name']}", callback_data=f"adm_ch:{ch['id']}")])
            buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "📢 Каналов пока нет"
            buttons = [
                [InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
            ]
        
        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_channels: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data.startswith("adm_ch:"))
async def adm_channel_settings(callback: CallbackQuery):
    """Настройки канала"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        channel_id = int(callback.data.split(":")[1])
        text, is_active, ch_id = await get_channel_card(channel_id)
        
        if not text:
            await callback.message.answer("❌ Канал не найден")
            return
        
        await safe_edit_message(
            callback.message,
            text,
            get_channel_settings_keyboard(channel_id, is_active)
        )
    except Exception as e:
        logger.error(f"Error in adm_channel_settings: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


# ==================== ИЗМЕНЕНИЕ ЦЕН ====================

@router.callback_query(F.data.startswith("adm_ch_prices:"))
async def adm_channel_prices(callback: CallbackQuery, state: FSMContext):
    """Изменить цены канала"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        channel_id = int(callback.data.split(":")[1])
        
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await callback.message.answer("❌ Канал не найден")
                return
            prices = channel.prices or {}
            channel_name = channel.name
        
        await state.update_data(editing_channel_id=channel_id)
        
        text = (
            f"💰 **Изменение цен для {channel_name}**\n\n"
            f"Текущие цены:\n"
            f"• 1/24: {prices.get('1/24', 0):,}₽\n"
            f"• 1/48: {prices.get('1/48', 0):,}₽\n"
            f"• 2/48: {prices.get('2/48', 0):,}₽\n"
            f"• Навсегда: {prices.get('native', 0):,}₽\n\n"
            f"Выберите что изменить:"
        )
        
        await safe_edit_message(
            callback.message,
            text,
            InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="1/24", callback_data=f"set_price:1/24:{channel_id}"),
                    InlineKeyboardButton(text="1/48", callback_data=f"set_price:1/48:{channel_id}")
                ],
                [
                    InlineKeyboardButton(text="2/48", callback_data=f"set_price:2/48:{channel_id}"),
                    InlineKeyboardButton(text="Навсегда", callback_data=f"set_price:native:{channel_id}")
                ],
                [InlineKeyboardButton(text="📊 Авторасчёт по CPM", callback_data=f"auto_prices:{channel_id}")],
                [InlineKeyboardButton(text="✏️ Ввести CPM вручную", callback_data=f"set_channel_cpm:{channel_id}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_ch:{channel_id}")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_channel_prices: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data.startswith("set_price:"))
async def set_price_start(callback: CallbackQuery, state: FSMContext):
    """Начать ввод цены"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    parts = callback.data.split(":")
    price_type = parts[1]
    channel_id = int(parts[2])
    
    await state.update_data(editing_channel_id=channel_id, editing_price_type=price_type)
    
    price_names = {"1/24": "1/24 (24 часа)", "1/48": "1/48 (48 часов)", "2/48": "2/48 (2 поста)", "native": "Навсегда"}
    
    await safe_edit_message(
        callback.message,
        f"💰 **Введите новую цену для {price_names.get(price_type, price_type)}**\n\nОтправьте число:",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_ch_prices:{channel_id}")]
        ])
    )
    await state.set_state(AdminChannelStates.waiting_price)


@router.message(AdminChannelStates.waiting_price)
async def receive_new_price(message: Message, state: FSMContext):
    """Получить новую цену"""
    try:
        new_price = int(message.text.strip().replace(" ", "").replace(",", ""))
    except:
        await message.answer("❌ Введите число!")
        return
    
    if new_price < 0:
        await message.answer("❌ Цена не может быть отрицательной!")
        return
    
    data = await state.get_data()
    channel_id = data.get("editing_channel_id")
    price_type = data.get("editing_price_type")
    
    if not channel_id or not price_type:
        await message.answer("❌ Ошибка. Начните заново.")
        await state.clear()
        return
    
    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await message.answer("❌ Канал не найден")
                await state.clear()
                return
            
            prices = dict(channel.prices) if channel.prices else {}
            prices[price_type] = new_price
            channel.prices = prices
            await session.commit()
        
        await state.clear()
        
        await message.answer(
            f"✅ Цена **{price_type}** установлена: **{new_price:,}₽**",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 Продолжить", callback_data=f"adm_ch_prices:{channel_id}")],
                [InlineKeyboardButton(text="◀️ К каналу", callback_data=f"adm_ch:{channel_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in receive_new_price: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)
        await state.clear()


@router.callback_query(F.data.startswith("auto_prices:"))
async def auto_calculate_prices(callback: CallbackQuery):
    """Авторасчёт цен по CPM"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer("📊 Рассчитываю...")
    
    channel_id = int(callback.data.split(":")[1])
    
    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await callback.message.answer("❌ Канал не найден")
                return
            
            category_info = CHANNEL_CATEGORIES.get(channel.category, {"cpm": 1000})
            cpm = float(channel.cpm or category_info.get("cpm", 1000))
            avg_reach_24h = channel.avg_reach_24h or channel.avg_reach or 0
            avg_reach_48h = channel.avg_reach_48h or 0
            
            if avg_reach_24h == 0:
                await callback.answer("❌ Нет данных об охвате!", show_alert=True)
                return
            
            # 1/24: базовая цена по охвату 24ч
            price_124 = int(avg_reach_24h * cpm / 1000)
            # 1/48: по охвату 48ч (если доступен) или 1.5x от 1/24
            if avg_reach_48h > 0:
                price_148 = int(avg_reach_48h * cpm / 1000)
            else:
                price_148 = int(price_124 * 1.5)
            # 2/48: два поста = 2x от 1/24
            price_248 = int(price_124 * 2.0)
            # Навсегда: 2.5x от 1/24
            price_native = int(price_124 * 2.5)
            
            channel.prices = {"1/24": price_124, "1/48": price_148, "2/48": price_248, "native": price_native}
            await session.commit()
        
        reach_info = f"📊 Охват 24ч: {avg_reach_24h:,}"
        if avg_reach_48h > 0:
            reach_info += f"\n📊 Охват 48ч: {avg_reach_48h:,}"
        
        await safe_edit_message(
            callback.message,
            f"✅ **Цены рассчитаны по CPM!**\n\n"
            f"{reach_info}\n"
            f"💰 CPM: {cpm:,.0f}₽\n\n"
            f"**Новые цены:**\n"
            f"• 1/24: {price_124:,}₽\n"
            f"• 1/48: {price_148:,}₽\n"
            f"• 2/48: {price_248:,}₽\n"
            f"• Навсегда: {price_native:,}₽",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К каналу", callback_data=f"adm_ch:{channel_id}")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in auto_calculate_prices: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data.startswith("set_channel_cpm:"))
async def set_channel_cpm_start(callback: CallbackQuery, state: FSMContext):
    """Начать ручной ввод CPM для конкретного канала"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    channel_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await callback.answer("❌ Канал не найден", show_alert=True)
                return
            current_cpm = float(channel.cpm or 0)
            channel_name = channel.name

        await state.update_data(editing_channel_id=channel_id, editing_price_type="cpm")
        await safe_edit_message(
            callback.message,
            f"✏️ **Ввести CPM вручную для {channel_name}**\n\n"
            f"Текущий CPM: **{current_cpm:,.0f}₽**\n\n"
            f"Введите новую цену за 1000 просмотров (CPM) в рублях:",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_ch_prices:{channel_id}")]
            ])
        )
        await state.set_state(AdminChannelStates.waiting_cpm)
    except Exception as e:
        logger.error(f"Error in set_channel_cpm_start: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.message(AdminChannelStates.waiting_cpm)
async def receive_channel_cpm(message: Message, state: FSMContext):
    """Сохранить новый CPM для канала"""
    try:
        new_cpm = float(message.text.strip().replace(" ", "").replace(",", ""))
    except (ValueError, AttributeError):
        await message.answer("❌ Введите число!")
        return

    if new_cpm < 0:
        await message.answer("❌ CPM не может быть отрицательным!")
        return

    data = await state.get_data()
    channel_id = data.get("editing_channel_id")
    if not channel_id:
        await message.answer("❌ Ошибка. Начните заново.")
        await state.clear()
        return

    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await message.answer("❌ Канал не найден")
                await state.clear()
                return
            channel.cpm = new_cpm
            await session.commit()

        await state.clear()
        await message.answer(
            f"✅ CPM канала установлен: **{new_cpm:,.0f}₽** за 1000 просмотров\n\n"
            f"Используйте «📊 Авторасчёт по CPM» для пересчёта цен.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К ценам канала", callback_data=f"adm_ch_prices:{channel_id}")],
                [InlineKeyboardButton(text="📊 Авторасчёт", callback_data=f"auto_prices:{channel_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in receive_channel_cpm: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)
        await state.clear()


# ==================== ОБНОВЛЕНИЕ СТАТИСТИКИ ====================

@router.callback_query(F.data.startswith("adm_ch_update:"))
async def adm_update_channel_stats(callback: CallbackQuery, bot: Bot):
    """Обновить статистику канала напрямую через Bot API (без сторонних сервисов)"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    channel_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await callback.answer("❌ Канал не найден", show_alert=True)
                return

        from services.channel_collector import (
            refresh_channel_subscribers,
            update_channel_reach_from_analytics,
        )

        # 1. Обновляем имя и число подписчиков через Bot API
        try:
            chat = await bot.get_chat(channel.telegram_id)
            async with async_session_maker() as session:
                ch = await session.get(Channel, channel_id)
                if ch:
                    ch.name = chat.title or ch.name
                    await session.commit()
        except TelegramBadRequest:
            await callback.answer("❌ Бот не является администратором канала", show_alert=True)
            return

        member_count = await refresh_channel_subscribers(bot, channel)

        # 2. Пересчитываем avg_reach и ERR из накопленных PostAnalytics
        reach_data = await update_channel_reach_from_analytics(channel_id)

        # Формируем сообщение об обновлении
        parts = []
        if member_count is not None:
            parts.append(f"👥 {member_count:,} подписчиков")
        if reach_data.get("records_used", 0) > 0:
            parts.append(
                f"👁 Охват {reach_data['avg_reach']:,} "
                f"| ERR {reach_data['err_percent']:.1f}% "
                f"(по {reach_data['records_used']} постам)"
            )
        else:
            parts.append("📊 Охват обновится после накопления аналитики постов")

        await callback.answer(" | ".join(parts) if parts else "✅ Обновлено", show_alert=True)

        # 3. Обновляем карточку канала
        text, is_active, _ = await get_channel_card(channel_id)
        if text:
            await safe_edit_message(callback.message, text, get_channel_settings_keyboard(channel_id, is_active))

    except Exception as e:
        logger.error(f"Error in adm_update_channel_stats: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== ВКЛ/ВЫКЛ КАНАЛ ====================

@router.callback_query(F.data.startswith("adm_ch_toggle:"))
async def adm_toggle_channel(callback: CallbackQuery):
    """Включить/выключить канал"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    channel_id = int(callback.data.split(":")[1])
    
    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if channel:
                channel.is_active = not channel.is_active
                await session.commit()
                status = "✅ Активирован" if channel.is_active else "❌ Деактивирован"
                await callback.answer(status, show_alert=True)
        
        text, is_active, _ = await get_channel_card(channel_id)
        if text:
            await safe_edit_message(callback.message, text, get_channel_settings_keyboard(channel_id, is_active))
    except Exception as e:
        logger.error(f"Error in adm_toggle_channel: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== УПРАВЛЕНИЕ СЛОТАМИ КАНАЛА ====================

@router.callback_query(F.data.startswith("adm_ch_slots:"))
async def adm_channel_slots(callback: CallbackQuery):
    """Просмотр слотов канала"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    channel_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await callback.answer("❌ Канал не найден", show_alert=True)
                return

            slots_result = await session.execute(
                select(Slot)
                .where(Slot.channel_id == channel_id, Slot.slot_date >= date_type.today())
                .order_by(Slot.slot_date, Slot.slot_time)
                .limit(30)
            )
            slots = slots_result.scalars().all()

        channel_name = channel.name
        if not slots:
            text = f"📅 **Слоты канала {channel_name}**\n\n_Нет предстоящих слотов._"
        else:
            text = f"📅 **Слоты канала {channel_name}** ({len(slots)}):\n\n"
            for s in slots:
                icon = "✅" if s.status == "available" else "🔒"
                text += f"{icon} {s.slot_date.strftime('%d.%m')} {s.slot_time.strftime('%H:%M')} — {s.status}\n"

        buttons = [
            [InlineKeyboardButton(text="➕ Сгенерировать слоты", callback_data=f"adm_slots_gen:{channel_id}")],
        ]
        if slots:
            buttons.append([InlineKeyboardButton(
                text="🗑 Удалить все предстоящие слоты",
                callback_data=f"adm_slots_clear:{channel_id}"
            )])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_ch:{channel_id}")])

        await safe_edit_message(
            callback.message,
            text,
            InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    except Exception as e:
        logger.error(f"Error in adm_channel_slots: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_slots_gen:"))
async def adm_slots_gen_start(callback: CallbackQuery, state: FSMContext):
    """Начало генерации слотов — запрос параметров"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    channel_id = int(callback.data.split(":")[1])
    await state.update_data(slot_channel_id=channel_id)

    await safe_edit_message(
        callback.message,
        "📅 **Генерация слотов**\n\n"
        "Введите параметры в формате:\n"
        "`<кол-во дней> <время1> [время2] ...`\n\n"
        "Например: `14 09:00 12:00 18:00`\n"
        "_(14 дней, каждый день в 09:00, 12:00 и 18:00)_",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_ch_slots:{channel_id}")]
        ])
    )
    await state.set_state(AdminSlotStates.waiting_slot_config)


@router.message(AdminSlotStates.waiting_slot_config)
async def adm_slots_gen_create(message: Message, state: FSMContext):
    """Создание слотов по введённым параметрам"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return

    data = await state.get_data()
    channel_id = data.get("slot_channel_id")
    await state.clear()

    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            await message.answer(
                "❌ Неверный формат. Пример: `14 09:00 12:00 18:00`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        days = int(parts[0])
        if days < 1 or days > 90:
            await message.answer("❌ Количество дней должно быть от 1 до 90.")
            return

        times = []
        for t_str in parts[1:]:
            if ":" not in t_str:
                await message.answer(
                    f"❌ Неверный формат времени: `{t_str}`. Используйте формат ЧЧ:ММ (например: `09:00`).",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            h_str, m_str = t_str.split(":", 1)
            h, m = int(h_str), int(m_str)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                await message.answer(
                    f"❌ Недопустимое время: `{t_str}`. Часы — 0–23, минуты — 0–59.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            times.append(time_type(h, m))

        if not times:
            await message.answer("❌ Укажите хотя бы одно время.")
            return

        created = 0
        skipped = 0
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await message.answer("❌ Канал не найден.")
                return

            for day_offset in range(days):
                slot_date = date_type.today() + timedelta(days=day_offset)
                for t in times:
                    # Проверяем, существует ли уже слот на эту дату/время
                    existing = await session.execute(
                        select(Slot).where(
                            Slot.channel_id == channel_id,
                            Slot.slot_date == slot_date,
                            Slot.slot_time == t
                        )
                    )
                    if existing.scalar_one_or_none():
                        skipped += 1
                        continue

                    slot = Slot(
                        channel_id=channel_id,
                        slot_date=slot_date,
                        slot_time=t,
                        status="available"
                    )
                    session.add(slot)
                    created += 1

            await session.commit()

        text = (
            f"✅ **Слоты созданы**\n\n"
            f"📢 Канал: **{channel.name}**\n"
            f"📅 Дней: {days}\n"
            f"🕐 Времена: {', '.join(t.strftime('%H:%M') for t in times)}\n\n"
            f"Создано: **{created}** слотов\n"
            f"Пропущено (уже существуют): {skipped}"
        )
        await message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📅 Слоты канала", callback_data=f"adm_ch_slots:{channel_id}")],
                [InlineKeyboardButton(text="◀️ К каналу", callback_data=f"adm_ch:{channel_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except (ValueError, IndexError):
        await message.answer(
            "❌ Неверный формат. Пример: `14 09:00 12:00 18:00`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_slots_gen_create: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("adm_slots_clear:"))
async def adm_slots_clear(callback: CallbackQuery):
    """Удалить все предстоящие доступные слоты канала"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    channel_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            slots_result = await session.execute(
                select(Slot).where(
                    Slot.channel_id == channel_id,
                    Slot.slot_date >= date_type.today(),
                    Slot.status == "available"
                )
            )
            slots = slots_result.scalars().all()
            count = len(slots)
            for s in slots:
                await session.delete(s)
            await session.commit()

        channel_name = channel.name if channel else f"#{channel_id}"
        await callback.answer(f"🗑 Удалено {count} слотов", show_alert=True)
        # Обновляем экран слотов
        callback.data = f"adm_ch_slots:{channel_id}"
        await adm_channel_slots(callback)
    except Exception as e:
        logger.error(f"Error in adm_slots_clear: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== УДАЛЕНИЕ КАНАЛА ====================

@router.callback_query(F.data.startswith("adm_ch_delete:"))
async def adm_delete_channel(callback: CallbackQuery):
    """Удалить канал"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    channel_id = int(callback.data.split(":")[1])
    
    await safe_edit_message(
        callback.message,
        "⚠️ **Удалить канал?**\n\nЭто действие нельзя отменить!",
        InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"adm_ch_del_confirm:{channel_id}"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"adm_ch:{channel_id}")
            ]
        ])
    )


@router.callback_query(F.data.startswith("adm_ch_del_confirm:"))
async def adm_delete_channel_confirm(callback: CallbackQuery):
    """Подтвердить удаление"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    channel_id = int(callback.data.split(":")[1])
    
    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if channel:
                await session.delete(channel)
                await session.commit()
                await callback.answer("🗑 Канал удалён", show_alert=True)
        
        # Показываем список каналов
        async with async_session_maker() as session:
            result = await session.execute(select(Channel))
            channels = result.scalars().all()
            channels_data = [{"id": ch.id, "name": ch.name, "is_active": ch.is_active} for ch in channels]
        
        if channels_data:
            text = "📢 **Каналы:**\n\n"
            buttons = []
            for ch in channels_data:
                status = "✅" if ch["is_active"] else "❌"
                text += f"{status} **{ch['name']}**\n"
                buttons.append([InlineKeyboardButton(text=f"⚙️ {ch['name']}", callback_data=f"adm_ch:{ch['id']}")])
            buttons.append([InlineKeyboardButton(text="➕ Добавить", callback_data="adm_add_channel")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "📢 Каналов пока нет"
            buttons = [
                [InlineKeyboardButton(text="➕ Добавить", callback_data="adm_add_channel")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
            ]
        
        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_delete_channel_confirm: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== ДОБАВЛЕНИЕ КАНАЛА ====================

@router.callback_query(F.data == "adm_add_channel")
async def adm_add_channel(callback: CallbackQuery, state: FSMContext):
    """Добавить канал"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    await safe_edit_message(
        callback.message,
        "➕ **Добавление канала**\n\nПерешлите сообщение из канала или отправьте @username:",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_channels")]
        ])
    )
    await state.set_state(AdminChannelStates.waiting_channel_forward)


@router.message(AdminChannelStates.waiting_channel_forward)
async def receive_channel_forward(message: Message, state: FSMContext, bot: Bot):
    """Получить пересланное сообщение или username"""
    
    channel_id = None
    channel_name = None
    channel_username = None
    
    if message.forward_from_chat:
        channel_id = message.forward_from_chat.id
        channel_name = message.forward_from_chat.title
        channel_username = message.forward_from_chat.username
    elif message.text and message.text.startswith("@"):
        try:
            chat = await bot.get_chat(message.text)
            channel_id = chat.id
            channel_name = chat.title
            channel_username = chat.username
        except:
            await message.answer("❌ Канал не найден")
            return
    else:
        await message.answer("❌ Перешлите сообщение или отправьте @username")
        return
    
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.telegram_id == channel_id))
        if result.scalar_one_or_none():
            await message.answer(f"⚠️ Канал **{channel_name}** уже добавлен!", parse_mode=ParseMode.MARKDOWN)
            await state.clear()
            return
    
    try:
        member_count = await bot.get_chat_member_count(channel_id)
    except:
        member_count = 0
    
    await state.update_data(
        new_channel_id=channel_id,
        new_channel_name=channel_name,
        new_channel_username=channel_username,
        new_channel_subscribers=member_count
    )
    
    await message.answer(
        f"📢 **{channel_name}**\n👤 @{channel_username or '—'}\n👥 {member_count:,}\n\nВыберите тематику:",
        reply_markup=get_category_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminChannelStates.waiting_category)


@router.callback_query(F.data.startswith("cat:"), AdminChannelStates.waiting_category)
async def select_channel_category(callback: CallbackQuery, state: FSMContext):
    """Выбрать категорию канала"""
    await callback.answer()
    
    category = callback.data.split(":")[1]
    data = await state.get_data()
    category_info = CHANNEL_CATEGORIES.get(category, {"name": "Другое", "cpm": 1000})
    
    try:
        async with async_session_maker() as session:
            channel = Channel(
                telegram_id=data["new_channel_id"],
                name=data["new_channel_name"],
                username=data.get("new_channel_username"),
                subscribers=data.get("new_channel_subscribers", 0),
                category=category,
                cpm=category_info.get("cpm", 1000),
                prices={"1/24": 0, "1/48": 0, "2/48": 0, "native": 0},
                is_active=True
            )
            session.add(channel)
            await session.commit()
            channel_id = channel.id
        
        await state.clear()
        
        await safe_edit_message(
            callback.message,
            f"✅ **Канал добавлен!**\n\n📢 {data['new_channel_name']}\n📁 {category_info['name']}",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 Установить цены", callback_data=f"adm_ch_prices:{channel_id}")],
                [InlineKeyboardButton(text="◀️ К каналам", callback_data="adm_channels")]
            ])
        )
    except Exception as e:
        logger.error(f"Error adding channel: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)
        await state.clear()


# ==================== МЕНЕДЖЕРЫ ====================

@router.callback_query(F.data == "adm_managers")
async def adm_managers(callback: CallbackQuery):
    """Список менеджеров"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(Manager).order_by(Manager.total_sales.desc()))
            managers = result.scalars().all()
            
            managers_data = []
            for m in managers[:15]:
                level_info = MANAGER_LEVELS.get(m.level, MANAGER_LEVELS[1])
                managers_data.append({
                    "id": m.id,
                    "name": m.first_name or m.username or "Менеджер",
                    "emoji": level_info["emoji"],
                    "is_active": m.is_active,
                    "total_sales": m.total_sales or 0,
                    "total_earned": float(m.total_earned or 0)
                })
        
        if managers_data:
            text = "👥 **Менеджеры:**\n\n"
            buttons = []
            for m in managers_data:
                status = "✅" if m["is_active"] else "❌"
                text += f"{status} {m['emoji']} **{m['name']}** — {m['total_sales']} продаж\n"
                buttons.append([InlineKeyboardButton(text=f"⚙️ {m['name']}", callback_data=f"adm_mgr:{m['id']}")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "👥 Менеджеров пока нет"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
        
        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_managers: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== ОПЛАТЫ ====================

@router.callback_query(F.data == "adm_payments")
async def adm_payments(callback: CallbackQuery):
    """Оплаты на проверке"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Order).where(Order.status == "payment_uploaded").order_by(Order.created_at.desc())
            )
            orders = result.scalars().all()
            orders_data = [{"id": o.id, "price": float(o.final_price or 0)} for o in orders[:10]]
        
        if orders_data:
            text = f"💳 **Оплаты: {len(orders_data)}**\n\n"
            buttons = []
            for o in orders_data:
                text += f"• #{o['id']} — {o['price']:,.0f}₽\n"
                buttons.append([InlineKeyboardButton(text=f"📄 #{o['id']}", callback_data=f"adm_order:{o['id']}")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "✅ Нет оплат на проверке"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
        
        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_payments: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== МОДЕРАЦИЯ ====================

@router.callback_query(F.data == "adm_moderation")
async def adm_moderation(callback: CallbackQuery):
    """Посты на модерации"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledPost).where(ScheduledPost.status == "moderation").order_by(ScheduledPost.created_at.desc())
            )
            posts = result.scalars().all()
            
            posts_data = []
            for post in posts[:10]:
                channel = await session.get(Channel, post.channel_id)
                posts_data.append({"id": post.id, "channel_name": channel.name if channel else "N/A"})
        
        if posts_data:
            text = f"📝 **Модерация: {len(posts_data)}**\n\n"
            buttons = []
            for post in posts_data:
                text += f"• #{post['id']} — {post['channel_name']}\n"
                buttons.append([InlineKeyboardButton(text=f"📄 #{post['id']}", callback_data=f"adm_post:{post['id']}")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "✅ Нет постов на модерации"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
        
        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_moderation: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== СТАТИСТИКА ====================

async def _build_crm_stats_text() -> str:
    """Собрать сводную статистику CRM и вернуть готовый текст."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    month_start = today_start - timedelta(days=30)
    prev_month_start = month_start - timedelta(days=30)

    async with async_session_maker() as session:
        total_orders = (await session.execute(select(func.count(Order.id)))).scalar() or 0
        pending_orders = (await session.execute(
            select(func.count(Order.id)).where(Order.status == "pending")
        )).scalar() or 0
        payment_uploaded = (await session.execute(
            select(func.count(Order.id)).where(Order.status == "payment_uploaded")
        )).scalar() or 0
        confirmed_orders = (await session.execute(
            select(func.count(Order.id)).where(Order.status == "payment_confirmed")
        )).scalar() or 0
        cancelled_orders = (await session.execute(
            select(func.count(Order.id)).where(Order.status == "cancelled")
        )).scalar() or 0

        total_revenue = float((await session.execute(
            select(func.sum(Order.final_price)).where(Order.status == "payment_confirmed")
        )).scalar() or 0)
        revenue_today = float((await session.execute(
            select(func.sum(Order.final_price)).where(
                Order.status == "payment_confirmed",
                Order.paid_at >= today_start,
            )
        )).scalar() or 0)
        revenue_week = float((await session.execute(
            select(func.sum(Order.final_price)).where(
                Order.status == "payment_confirmed",
                Order.paid_at >= week_start,
            )
        )).scalar() or 0)
        revenue_month = float((await session.execute(
            select(func.sum(Order.final_price)).where(
                Order.status == "payment_confirmed",
                Order.paid_at >= month_start,
            )
        )).scalar() or 0)
        revenue_prev_month = float((await session.execute(
            select(func.sum(Order.final_price)).where(
                Order.status == "payment_confirmed",
                Order.paid_at >= prev_month_start,
                Order.paid_at < month_start,
            )
        )).scalar() or 0)

        orders_today = (await session.execute(
            select(func.count(Order.id)).where(Order.created_at >= today_start)
        )).scalar() or 0
        orders_week = (await session.execute(
            select(func.count(Order.id)).where(Order.created_at >= week_start)
        )).scalar() or 0
        orders_month = (await session.execute(
            select(func.count(Order.id)).where(Order.created_at >= month_start)
        )).scalar() or 0

        total_managers = (await session.execute(select(func.count(Manager.id)))).scalar() or 0
        active_managers = (await session.execute(
            select(func.count(Manager.id)).where(Manager.is_active == True)
        )).scalar() or 0
        total_channels = (await session.execute(select(func.count(Channel.id)))).scalar() or 0
        active_channels = (await session.execute(
            select(func.count(Channel.id)).where(Channel.is_active == True)
        )).scalar() or 0
        total_clients = (await session.execute(select(func.count(Client.id)))).scalar() or 0
        new_clients_month = (await session.execute(
            select(func.count(Client.id)).where(Client.created_at >= month_start)
        )).scalar() or 0

    if revenue_prev_month > 0:
        rev_change = (revenue_month - revenue_prev_month) / revenue_prev_month * 100
        rev_trend = f" ({'▲' if rev_change >= 0 else '▼'}{abs(rev_change):.1f}%)"
    else:
        rev_trend = ""

    conversion = round(confirmed_orders / total_orders * 100, 1) if total_orders > 0 else 0
    cancel_rate = round(cancelled_orders / total_orders * 100, 1) if total_orders > 0 else 0

    return (
        "📊 **Метрики CRM**\n\n"
        "💰 **Выручка:**\n"
        f"• Сегодня: **{revenue_today:,.0f}₽**\n"
        f"• За 7 дней: **{revenue_week:,.0f}₽**\n"
        f"• За 30 дней: **{revenue_month:,.0f}₽**{rev_trend}\n"
        f"• Всего: **{total_revenue:,.0f}₽**\n\n"
        "📦 **Заказы:**\n"
        f"• Сегодня: **{orders_today}** | Неделя: **{orders_week}** | Месяц: **{orders_month}**\n"
        f"• Всего: **{total_orders}** | Конверсия: **{conversion}%** | Отмены: **{cancel_rate}%**\n\n"
        "📋 **Статусы:**\n"
        f"• ⏳ Ожидают: **{pending_orders}** | 💳 На проверке: **{payment_uploaded}**\n"
        f"• ✅ Подтверждены: **{confirmed_orders}** | ❌ Отменены: **{cancelled_orders}**\n\n"
        "📢 **Каналы:** **{ac}** активных из **{tc}**\n"
        "👥 **Менеджеры:** **{am}** активных из **{tm}**\n"
        "🧑‍💼 **Клиенты:** **{tcl}** всего | +**{ncl}** за месяц"
    ).format(
        ac=active_channels, tc=total_channels,
        am=active_managers, tm=total_managers,
        tcl=total_clients, ncl=new_clients_month,
    )


@router.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    """Сводная статистика + навигация по метрикам"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        text = await _build_crm_stats_text()
        from keyboards.menus import get_metrics_menu
        await safe_edit_message(callback.message, text, get_metrics_menu())
    except Exception as e:
        logger.error(f"Error in adm_stats: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== ДЕТАЛЬНЫЕ МЕТРИКИ ====================

_METRICS_BACK = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="◀️ К метрикам", callback_data="adm_stats")]
])


@router.callback_query(F.data.startswith("metrics_sales:"))
async def metrics_sales(callback: CallbackQuery):
    """Метрики продаж с переключением периода"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    period = callback.data.split(":")[1]
    await callback.answer()

    try:
        from services.metrics import get_sales_metrics
        from keyboards.menus import get_sales_period_keyboard

        data = await get_sales_metrics(period)
        if not data:
            await safe_edit_message(
                callback.message,
                "❌ Не удалось получить метрики продаж. Проверьте соединение с базой данных.",
                _METRICS_BACK,
            )
            return

        period_labels = {"day": "день", "week": "неделю", "month": "месяц"}
        label = period_labels.get(period, period)

        text = (
            f"💰 **Метрики продаж — за {label}**\n\n"
            f"📈 Выручка: **{data['revenue']:,.0f}₽**{data['revenue_delta']}\n"
            f"  (предыдущий период: {data['revenue_prev']:,.0f}₽)\n\n"
            f"📦 Заказов: **{data['orders']}**{data['orders_delta']}\n"
            f"  (предыдущий период: {data['orders_prev']})\n"
            f"  ✅ Подтверждено: **{data['confirmed']}**\n\n"
            f"💵 Средний чек: **{data['avg_order_value']:,.0f}₽**\n"
            f"🎯 Конверсия: **{data['conversion_rate']}%**\n"
            f"❌ Доля отмен: **{data['cancel_rate']}%**\n\n"
            f"🧑‍💼 Новых клиентов: **{data['new_clients']}**{data['new_clients_delta']}\n"
            f"  (предыдущий период: {data['new_clients_prev']})"
        )
        await safe_edit_message(callback.message, text, get_sales_period_keyboard(active=period))
    except Exception:
        logger.error(f"Error in metrics_sales: {traceback.format_exc()}")
        await safe_edit_message(
            callback.message,
            "❌ Ошибка при загрузке метрик продаж. Попробуйте ещё раз.",
            _METRICS_BACK,
        )


@router.callback_query(F.data == "metrics_channels")
async def metrics_channels(callback: CallbackQuery):
    """Метрики каналов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        from services.metrics import get_channel_metrics

        data = await get_channel_metrics()
        if not data:
            await safe_edit_message(
                callback.message,
                "❌ Не удалось получить метрики каналов. Проверьте соединение с базой данных.",
                _METRICS_BACK,
            )
            return

        text = (
            "📢 **Метрики каналов**\n\n"
            f"🔢 Активных каналов: **{data['total_active']}**\n"
            f"💰 Средний CPM: **{data['avg_cpm']:,.0f}₽**\n"
            f"📊 Средний ERR: **{data['avg_err']:.1f}%**\n"
            f"👁 Средний охват (24ч): **{data['avg_reach']:,}**\n"
            f"📝 Постов с аналитикой: **{data['analytics_posts']}** "
            f"(~{data['total_views_tracked']:,} просмотров)\n\n"
        )

        if data["top_by_revenue"]:
            text += "🏆 **Топ каналов по выручке:**\n"
            for i, (name, rev, cnt) in enumerate(data["top_by_revenue"], 1):
                text += f"{i}. {_md_escape(name)}\n   💰 {rev:,.0f}₽ | 📦 {cnt} заказов\n"
        else:
            text += "_Нет данных по выручке каналов_\n"

        await safe_edit_message(callback.message, text, _METRICS_BACK)
    except Exception:
        logger.error(f"Error in metrics_channels: {traceback.format_exc()}")
        await safe_edit_message(
            callback.message,
            "❌ Ошибка при загрузке метрик каналов. Попробуйте ещё раз.",
            _METRICS_BACK,
        )


@router.callback_query(F.data == "metrics_managers")
async def metrics_managers(callback: CallbackQuery):
    """Метрики менеджеров"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        from services.metrics import get_manager_metrics

        data = await get_manager_metrics()
        if not data:
            await safe_edit_message(
                callback.message,
                "❌ Не удалось получить метрики менеджеров. Проверьте соединение с базой данных.",
                _METRICS_BACK,
            )
            return

        text = (
            "👥 **Метрики менеджеров**\n\n"
            f"💵 Средний чек менеджера: **{data['avg_check']:,.0f}₽**\n\n"
        )

        if data["top_revenue"]:
            text += "🏆 **Топ по выручке:**\n"
            for i, m in enumerate(data["top_revenue"], 1):
                text += f"{i}. {_md_escape(m['name'])} — {m['revenue']:,.0f}₽ ({m['orders']} заказов)\n"
            text += "\n"

        if data["top_conversion"]:
            text += "🎯 **Топ по конверсии (мин. 3 заказа):**\n"
            for i, m in enumerate(data["top_conversion"], 1):
                text += f"{i}. {_md_escape(m['name'])} — {m['rate']}% ({m['confirmed']}/{m['total']})\n"

        if not data["top_revenue"] and not data["top_conversion"]:
            text += "_Недостаточно данных_"

        await safe_edit_message(callback.message, text, _METRICS_BACK)
    except Exception:
        logger.error(f"Error in metrics_managers: {traceback.format_exc()}")
        await safe_edit_message(
            callback.message,
            "❌ Ошибка при загрузке метрик менеджеров. Попробуйте ещё раз.",
            _METRICS_BACK,
        )


@router.callback_query(F.data == "metrics_clients")
async def metrics_clients(callback: CallbackQuery):
    """Метрики клиентов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        from services.metrics import get_client_metrics

        data = await get_client_metrics()
        if not data:
            await safe_edit_message(
                callback.message,
                "❌ Не удалось получить метрики клиентов. Проверьте соединение с базой данных.",
                _METRICS_BACK,
            )
            return

        text = (
            "🧑‍💼 **Метрики клиентов**\n\n"
            f"👥 Всего клиентов: **{data['total']}**\n"
            f"🆕 Новых за 30 дней: **{data['new_month']}**\n"
            f"🔄 Повторные покупатели: **{data['repeat']}** ({data['repeat_rate']}%)\n\n"
            f"💰 Средний LTV: **{data['avg_ltv']:,.0f}₽**\n"
            f"📦 Среднее заказов/клиент: **{data['avg_orders_per_client']}**\n\n"
        )

        if data["top"]:
            text += "🏆 **Топ клиентов по тратам:**\n"
            for i, c in enumerate(data["top"], 1):
                text += f"{i}. {_md_escape(c['name'])} — {c['spent']:,.0f}₽ ({c['orders']} заказов)\n"
        else:
            text += "_Нет данных_"

        await safe_edit_message(callback.message, text, _METRICS_BACK)
    except Exception:
        logger.error(f"Error in metrics_clients: {traceback.format_exc()}")
        await safe_edit_message(
            callback.message,
            "❌ Ошибка при загрузке метрик клиентов. Попробуйте ещё раз.",
            _METRICS_BACK,
        )


@router.callback_query(F.data == "metrics_formats")
async def metrics_formats(callback: CallbackQuery):
    """Метрики по форматам размещения"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        from services.metrics import get_format_metrics

        data = await get_format_metrics()
        if not data:
            await safe_edit_message(
                callback.message,
                "❌ Не удалось получить метрики форматов. Проверьте соединение с базой данных.",
                _METRICS_BACK,
            )
            return

        text = (
            "🗂 **Метрики форматов размещения**\n\n"
            f"Всего подтверждённых заказов: **{data['total_orders']}**\n"
            f"Общая выручка: **{data['total_revenue']:,.0f}₽**\n\n"
        )

        if data["formats"]:
            for fmt_item in data["formats"]:
                bar = "█" * int(fmt_item["revenue_share"] / 10) + "░" * (10 - int(fmt_item["revenue_share"] / 10))
                text += (
                    f"**{_md_escape(fmt_item['type'])}**\n"
                    f"  {bar} {fmt_item['revenue_share']}% выручки\n"
                    f"  📦 {fmt_item['orders']} заказов ({fmt_item['order_share']}%) | 💰 {fmt_item['revenue']:,.0f}₽\n\n"
                )
        else:
            text += "_Нет данных_"

        await safe_edit_message(callback.message, text, _METRICS_BACK)
    except Exception:
        logger.error(f"Error in metrics_formats: {traceback.format_exc()}")
        await safe_edit_message(
            callback.message,
            "❌ Ошибка при загрузке метрик форматов. Попробуйте ещё раз.",
            _METRICS_BACK,
        )


@router.callback_query(F.data == "metrics_posts")
async def metrics_posts(callback: CallbackQuery):
    """Метрики аналитики постов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        from services.metrics import get_post_analytics_metrics

        data = await get_post_analytics_metrics()
        if not data:
            await safe_edit_message(
                callback.message,
                "❌ Не удалось получить метрики постов. Проверьте соединение с базой данных.",
                _METRICS_BACK,
            )
            return

        if data["count"] == 0:
            text = (
                "📈 **Аналитика постов**\n\n"
                "Данных пока нет.\nВнесите метрики для опубликованных постов в разделе Автопостинг."
            )
        else:
            text = (
                "📈 **Аналитика постов**\n\n"
                f"📝 Записей аналитики: **{data['count']}**\n"
                f"👁 Среднее просмотров: **{data['avg_views']:,}**\n"
                f"👍 Среднее реакций: **{data['avg_reactions']:.1f}**\n"
                f"📊 Средний ER: **{data['avg_er']}%**\n\n"
            )
            if data["top"]:
                text += "🏆 **Топ-5 постов по ER:**\n"
                for i, p in enumerate(data["top"], 1):
                    text += f"{i}. {_md_escape(p['channel'])} — ER {p['er']}% | 👁 {p['views']:,}\n"

        await safe_edit_message(callback.message, text, _METRICS_BACK)
    except Exception:
        logger.error(f"Error in metrics_posts: {traceback.format_exc()}")
        await safe_edit_message(
            callback.message,
            "❌ Ошибка при загрузке метрик постов. Попробуйте ещё раз.",
            _METRICS_BACK,
        )


# ==================== СОРЕВНОВАНИЯ ====================

@router.callback_query(F.data == "adm_competitions")
async def adm_competitions(callback: CallbackQuery):
    """Соревнования"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Competition).where(Competition.status == "active").order_by(Competition.start_date.desc())
            )
            competitions = result.scalars().all()
        
        if competitions:
            text = "🏆 **Соревнования:**\n\n"
            for c in competitions:
                text += f"• {c.name}\n  📅 {c.start_date} — {c.end_date}\n\n"
        else:
            text = "🏆 Нет активных соревнований"
        
        await safe_edit_message(
            callback.message, text,
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать", callback_data="adm_create_competition")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_competitions: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== CPM ====================

@router.callback_query(F.data == "adm_cpm")
async def adm_cpm(callback: CallbackQuery):
    """CPM по тематикам — страница 0"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    await callback.answer()
    await _show_cpm_page(callback, page=0)


@router.callback_query(F.data.startswith("cpm_page:"))
async def adm_cpm_page(callback: CallbackQuery):
    """Пагинация списка CPM"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    await callback.answer()
    page = int(callback.data.split(":")[1])
    await _show_cpm_page(callback, page=page)


async def _show_cpm_page(callback: CallbackQuery, page: int):
    """Вспомогательная функция: показать страницу CPM"""
    PER_PAGE = 10
    # Загружаем переопределения из БД
    overrides: dict = {}
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(CategoryCPM))
            for row in result.scalars():
                overrides[row.category_key] = row.cpm
    except Exception:
        pass

    sorted_categories = sorted(CHANNEL_CATEGORIES.items(), key=lambda x: x[1]["cpm"], reverse=True)
    # Применяем переопределения
    effective = [(k, {"name": v["name"], "cpm": overrides.get(k, v["cpm"])}) for k, v in sorted_categories]

    total = len(effective)
    start = page * PER_PAGE
    text = (
        f"💰 **CPM по тематикам** (стр. {page + 1}/{(total + PER_PAGE - 1) // PER_PAGE})\n\n"
        "Нажмите ✏️ рядом с тематикой, чтобы изменить CPM вручную."
    )

    await safe_edit_message(
        callback.message, text,
        get_cpm_categories_keyboard(effective, page=page, per_page=PER_PAGE)
    )


@router.callback_query(F.data.startswith("cpm_info:"))
async def adm_cpm_info(callback: CallbackQuery):
    """Показать детали CPM тематики"""
    await callback.answer()


@router.callback_query(F.data.startswith("cpm_edit:"))
async def adm_cpm_edit_start(callback: CallbackQuery, state: FSMContext):
    """Начать ручной ввод CPM для тематики"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    category_key = callback.data.split(":")[1]
    cat_info = CHANNEL_CATEGORIES.get(category_key)
    if not cat_info:
        await callback.answer("❌ Тематика не найдена", show_alert=True)
        return

    # Проверяем текущее переопределение в БД
    current_cpm = cat_info["cpm"]
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(CategoryCPM).where(CategoryCPM.category_key == category_key)
            )
            db_row = result.scalar_one_or_none()
            if db_row:
                current_cpm = db_row.cpm
    except Exception:
        pass

    await state.update_data(editing_cpm_key=category_key, editing_cpm_name=cat_info["name"])
    await safe_edit_message(
        callback.message,
        f"✏️ **Изменить CPM: {cat_info['name']}**\n\n"
        f"Текущий CPM: **{current_cpm:,}₽**\n\n"
        f"Введите новое значение CPM (руб. за 1000 просмотров):",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cpm")]
        ])
    )
    await state.set_state(AdminCPMStates.waiting_cpm_value)


@router.message(AdminCPMStates.waiting_cpm_value)
async def adm_cpm_receive_value(message: Message, state: FSMContext):
    """Сохранить новое значение CPM для тематики"""
    try:
        new_cpm = int(message.text.strip().replace(" ", "").replace(",", ""))
    except (ValueError, AttributeError):
        await message.answer("❌ Введите целое число!")
        return

    if new_cpm < 0:
        await message.answer("❌ CPM не может быть отрицательным!")
        return

    data = await state.get_data()
    category_key = data.get("editing_cpm_key")
    category_name = data.get("editing_cpm_name", "")

    if not category_key:
        await message.answer("❌ Ошибка. Начните заново.")
        await state.clear()
        return

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(CategoryCPM).where(CategoryCPM.category_key == category_key)
            )
            db_row = result.scalar_one_or_none()
            if db_row:
                db_row.cpm = new_cpm
                db_row.updated_at = datetime.utcnow()
                db_row.updated_by = message.from_user.id
            else:
                session.add(CategoryCPM(
                    category_key=category_key,
                    name=category_name,
                    cpm=new_cpm,
                    updated_by=message.from_user.id,
                ))
            await session.commit()

        await state.clear()
        await message.answer(
            f"✅ CPM для **{category_name}** установлен: **{new_cpm:,}₽**",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К CPM тематик", callback_data="adm_cpm")],
                [InlineKeyboardButton(text="◀️ В панель", callback_data="adm_back")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error saving CPM: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)
        await state.clear()


# ==================== АВТОПОСТИНГ ====================

@router.callback_query(F.data == "adm_autoposting")
async def adm_autoposting(callback: CallbackQuery):
    """Раздел Автопостинг"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        async with async_session_maker() as session:
            pending_count = (await session.execute(
                select(func.count(ScheduledPost.id)).where(ScheduledPost.status == "pending")
            )).scalar() or 0
            posted_count = (await session.execute(
                select(func.count(ScheduledPost.id)).where(ScheduledPost.status.in_(["posted", "deleted"]))
            )).scalar() or 0
            analytics_count = (await session.execute(
                select(func.count(PostAnalytics.id))
            )).scalar() or 0

        text = (
            "📅 **Автопостинг**\n\n"
            f"📋 Запланированных постов: **{pending_count}**\n"
            f"✅ Опубликованных постов: **{posted_count}**\n"
            f"📊 Записей аналитики: **{analytics_count}**\n\n"
            "Выберите раздел:"
        )
        await safe_edit_message(callback.message, text, get_autoposting_menu())
    except Exception as e:
        logger.error(f"Error in adm_autoposting: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "autopost_pending")
async def autopost_pending(callback: CallbackQuery):
    """Список запланированных постов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledPost)
                .where(ScheduledPost.status.in_(["pending", "moderation"]))
                .order_by(ScheduledPost.scheduled_time.asc())
                .limit(20)
            )
            posts = result.scalars().all()

            if not posts:
                await safe_edit_message(
                    callback.message,
                    "📋 **Запланированных постов нет**",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")]
                    ])
                )
                return

            text = f"📋 **Запланированные посты** ({len(posts)})\n\n"
            buttons = []
            for post in posts:
                channel = await session.get(Channel, post.channel_id)
                ch_name = channel.name if channel else f"#{post.channel_id}"
                sched = (post.scheduled_time + LOCAL_TZ_OFFSET).strftime("%d.%m %H:%M") if post.scheduled_time else "—"
                status_icon = "⏳" if post.status == "pending" else "🔍"
                text_preview = (post.content[:30] + "…") if post.content else "📎 медиа"
                buttons.append([InlineKeyboardButton(
                    text=f"{status_icon} {ch_name} | {sched} | {text_preview}",
                    callback_data=f"adm_post:{post.id}"
                )])

            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")])
            await safe_edit_message(
                callback.message, text,
                InlineKeyboardMarkup(inline_keyboard=buttons)
            )
    except Exception as e:
        logger.error(f"Error in autopost_pending: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "autopost_posted")
async def autopost_posted(callback: CallbackQuery):
    """Список опубликованных постов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledPost)
                .where(ScheduledPost.status.in_(["posted", "deleted"]))
                .order_by(ScheduledPost.posted_at.desc())
                .limit(20)
            )
            posts = result.scalars().all()

            if not posts:
                await safe_edit_message(
                    callback.message,
                    "✅ **Опубликованных постов нет**",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")]
                    ])
                )
                return

            text = f"✅ **Опубликованные посты** ({len(posts)})\n\n"
            buttons = []
            for post in posts:
                channel = await session.get(Channel, post.channel_id)
                ch_name = channel.name if channel else f"#{post.channel_id}"
                posted = post.posted_at.strftime("%d.%m %H:%M") if post.posted_at else "—"
                text_preview = (post.content[:30] + "…") if post.content else "📎 медиа"
                status_icon = "✅" if post.status == "posted" else "🗑"
                buttons.append([InlineKeyboardButton(
                    text=f"{status_icon} {ch_name} | {posted} | {text_preview}",
                    callback_data=f"autopost_view_posted:{post.id}"
                )])

            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")])
            await safe_edit_message(
                callback.message, text,
                InlineKeyboardMarkup(inline_keyboard=buttons)
            )
    except Exception as e:
        logger.error(f"Error in autopost_posted: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("autopost_view_posted:"))
async def autopost_view_posted(callback: CallbackQuery):
    """Просмотр опубликованного поста с возможностью добавить аналитику"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        post_id = int(callback.data.split(":")[1])
        async with async_session_maker() as session:
            post = await session.get(ScheduledPost, post_id)
            if not post:
                await callback.answer("❌ Пост не найден", show_alert=True)
                return

            channel = await session.get(Channel, post.channel_id)
            ch_name = channel.name if channel else "—"

            # Проверяем, есть ли уже аналитика
            existing = (await session.execute(
                select(PostAnalytics).where(PostAnalytics.scheduled_post_id == post_id)
            )).scalar_one_or_none()

        posted = post.posted_at.strftime("%d.%m.%Y %H:%M") if post.posted_at else "—"
        text = (
            f"✅ **Пост #{post_id}**\n\n"
            f"📢 Канал: {ch_name}\n"
            f"📅 Опубликован: {posted}\n"
        )
        if post.content:
            text += f"\n📝 Текст:\n{post.content[:300]}{'…' if len(post.content) > 300 else ''}\n"

        buttons = []
        if existing:
            text += (
                f"\n📊 **Аналитика:**\n"
                f"👁 Просмотры: {existing.views}\n"
                f"👍 Реакции: {existing.reactions}\n"
                f"↩️ Пересылки: {existing.forwards}\n"
                f"🔖 Сохранения: {existing.saves}\n"
            )
            if existing.ai_recommendation:
                text += f"\n🤖 **AI-рекомендация:**\n{existing.ai_recommendation[:400]}\n"
            buttons.append([InlineKeyboardButton(
                text="📊 Обновить метрики",
                callback_data=f"pa_enter:{post_id}"
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text="📊 Внести метрики поста",
                callback_data=f"pa_enter:{post_id}"
            )])

        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="autopost_posted")])
        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in autopost_view_posted: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== АНАЛИТИКА ПОСТОВ ====================

@router.callback_query(F.data == "autopost_analytics")
async def autopost_analytics(callback: CallbackQuery):
    """Список аналитики рекламных постов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(PostAnalytics).order_by(PostAnalytics.recorded_at.desc()).limit(20)
            )
            analytics_list = result.scalars().all()

        if not analytics_list:
            await safe_edit_message(
                callback.message,
                "📊 **Аналитика постов**\n\nЗаписей пока нет.\n\nДобавьте метрики к опубликованным постам.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Опубликованные посты", callback_data="autopost_posted")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")]
                ])
            )
            return

        text = f"📊 **Аналитика постов** ({len(analytics_list)})\n\n"
        await safe_edit_message(
            callback.message, text,
            get_post_analytics_keyboard(analytics_list)
        )
    except Exception as e:
        logger.error(f"Error in autopost_analytics: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("pa_view:"))
async def pa_view(callback: CallbackQuery):
    """Просмотр записи аналитики поста"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        analytics_id = int(callback.data.split(":")[1])
        async with async_session_maker() as session:
            analytics = await session.get(PostAnalytics, analytics_id)
            if not analytics:
                await callback.answer("❌ Запись не найдена", show_alert=True)
                return
            channel = await session.get(Channel, analytics.channel_id)

        ch_name = channel.name if channel else "—"
        recorded = analytics.recorded_at.strftime("%d.%m.%Y %H:%M")
        total_engage = analytics.reactions + analytics.forwards + analytics.saves + analytics.comments
        er = round(total_engage / analytics.views * 100, 2) if analytics.views > 0 else 0

        text = (
            f"📊 **Аналитика поста #{analytics_id}**\n\n"
            f"📢 Канал: {ch_name}\n"
            f"📅 Записано: {recorded}\n\n"
            f"👁 Просмотры: **{analytics.views:,}**\n"
            f"👍 Реакции: **{analytics.reactions:,}**\n"
            f"↩️ Пересылки: **{analytics.forwards:,}**\n"
            f"🔖 Сохранения: **{analytics.saves:,}**\n"
            f"💬 Комментарии: **{analytics.comments:,}**\n"
            f"📈 Engagement Rate: **{er}%**\n"
        )
        if analytics.ai_recommendation:
            text += f"\n🤖 **AI-рекомендация:**\n{analytics.ai_recommendation}\n"

        await safe_edit_message(
            callback.message, text,
            get_post_analytics_actions_keyboard(analytics_id, has_ai=bool(analytics.ai_recommendation))
        )
    except Exception as e:
        logger.error(f"Error in pa_view: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("pa_enter:"))
async def pa_enter_start(callback: CallbackQuery, state: FSMContext):
    """Начало ввода метрик поста"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    post_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            post = await session.get(ScheduledPost, post_id)
            if not post:
                await callback.answer("❌ Пост не найден", show_alert=True)
                return

        await state.update_data(pa_post_id=post_id, pa_channel_id=post.channel_id)
        await safe_edit_message(
            callback.message,
            "📊 **Ввод метрик поста**\n\n👁 Введите количество **просмотров**:",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"autopost_view_posted:{post_id}")]
            ])
        )
        await state.set_state(AdminAutopostingStates.waiting_post_views)
    except Exception as e:
        logger.error(f"Error in pa_enter_start: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.message(AdminAutopostingStates.waiting_post_views)
async def pa_receive_views(message: Message, state: FSMContext):
    """Получить просмотры"""
    try:
        views = int(message.text.strip().replace(" ", "").replace(",", ""))
        if views < 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("❌ Введите целое неотрицательное число!")
        return

    await state.update_data(pa_views=views)
    await message.answer(
        f"✅ Просмотры: {views:,}\n\n👍 Введите количество **реакций**:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="autopost_analytics")]
        ])
    )
    await state.set_state(AdminAutopostingStates.waiting_post_reactions)


@router.message(AdminAutopostingStates.waiting_post_reactions)
async def pa_receive_reactions(message: Message, state: FSMContext):
    """Получить реакции"""
    try:
        reactions = int(message.text.strip().replace(" ", "").replace(",", ""))
        if reactions < 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("❌ Введите целое неотрицательное число!")
        return

    await state.update_data(pa_reactions=reactions)
    await message.answer(
        f"✅ Реакции: {reactions:,}\n\n↩️ Введите количество **пересылок**:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="autopost_analytics")]
        ])
    )
    await state.set_state(AdminAutopostingStates.waiting_post_forwards)


@router.message(AdminAutopostingStates.waiting_post_forwards)
async def pa_receive_forwards(message: Message, state: FSMContext):
    """Получить пересылки"""
    try:
        forwards = int(message.text.strip().replace(" ", "").replace(",", ""))
        if forwards < 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("❌ Введите целое неотрицательное число!")
        return

    await state.update_data(pa_forwards=forwards)
    await message.answer(
        f"✅ Пересылки: {forwards:,}\n\n🔖 Введите количество **сохранений**:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="autopost_analytics")]
        ])
    )
    await state.set_state(AdminAutopostingStates.waiting_post_saves)


@router.message(AdminAutopostingStates.waiting_post_saves)
async def pa_receive_saves(message: Message, state: FSMContext):
    """Получить сохранения, запросить комментарии"""
    try:
        saves = int(message.text.strip().replace(" ", "").replace(",", ""))
        if saves < 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("❌ Введите целое неотрицательное число!")
        return

    await state.update_data(pa_saves=saves)
    await message.answer(
        f"✅ Сохранения: {saves:,}\n\n💬 Введите количество **комментариев**:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="autopost_analytics")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminAutopostingStates.waiting_post_comments)


@router.message(AdminAutopostingStates.waiting_post_comments)
async def pa_receive_comments(message: Message, state: FSMContext):
    """Получить комментарии и сохранить запись аналитики"""
    try:
        comments = int(message.text.strip().replace(" ", "").replace(",", ""))
        if comments < 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("❌ Введите целое неотрицательное число!")
        return

    data = await state.get_data()
    post_id = data.get("pa_post_id")
    channel_id = data.get("pa_channel_id")
    views = data.get("pa_views", 0)
    reactions = data.get("pa_reactions", 0)
    forwards = data.get("pa_forwards", 0)
    saves = data.get("pa_saves", 0)

    await state.clear()

    try:
        async with async_session_maker() as session:
            # Проверяем, есть ли уже запись для этого поста
            existing = (await session.execute(
                select(PostAnalytics).where(PostAnalytics.scheduled_post_id == post_id)
            )).scalar_one_or_none()

            if existing:
                existing.views = views
                existing.reactions = reactions
                existing.forwards = forwards
                existing.saves = saves
                existing.comments = comments
                existing.recorded_at = datetime.utcnow()
                existing.recorded_by = message.from_user.id
                existing.ai_recommendation = None  # Сбрасываем старую рекомендацию
                analytics = existing
            else:
                analytics = PostAnalytics(
                    scheduled_post_id=post_id,
                    channel_id=channel_id,
                    views=views,
                    reactions=reactions,
                    forwards=forwards,
                    saves=saves,
                    comments=comments,
                    recorded_by=message.from_user.id,
                )
                session.add(analytics)

            await session.commit()
            await session.refresh(analytics)
            analytics_id = analytics.id

        total_engage = reactions + forwards + saves + comments
        er = round(total_engage / views * 100, 2) if views > 0 else 0

        await message.answer(
            f"✅ **Метрики поста сохранены!**\n\n"
            f"👁 Просмотры: **{views:,}**\n"
            f"👍 Реакции: **{reactions:,}**\n"
            f"↩️ Пересылки: **{forwards:,}**\n"
            f"🔖 Сохранения: **{saves:,}**\n"
            f"💬 Комментарии: **{comments:,}**\n"
            f"📈 Engagement Rate: **{er}%**\n\n"
            f"Получите AI-рекомендации по этому посту:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🤖 AI-рекомендации", callback_data=f"pa_ai:{analytics_id}")],
                [InlineKeyboardButton(text="📊 Все аналитики", callback_data="autopost_analytics")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in pa_receive_comments: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data.startswith("pa_ai:"))
async def pa_ai_recommend(callback: CallbackQuery):
    """Получить AI-рекомендацию по аналитике поста"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer("🤖 Генерирую рекомендацию…")

    try:
        analytics_id = int(callback.data.split(":")[1])
        async with async_session_maker() as session:
            analytics = await session.get(PostAnalytics, analytics_id)
            if not analytics:
                await callback.answer("❌ Запись не найдена", show_alert=True)
                return
            channel = await session.get(Channel, analytics.channel_id)
            # Extract all values we need while still inside the session
            a_views = analytics.views or 0
            a_reactions = analytics.reactions or 0
            a_forwards = analytics.forwards or 0
            a_saves = analytics.saves or 0
            a_comments = analytics.comments or 0
            ch_name = channel.name if channel else "Канал"
            avg_views = int(channel.avg_reach or channel.avg_reach_24h or 0) if channel else 0
            cpm = float(channel.cpm or 0) if channel else 0

        recommendation = await ai_trainer_service.get_post_recommendations(
            channel_name=ch_name,
            views=a_views,
            reactions=a_reactions,
            forwards=a_forwards,
            saves=a_saves,
            comments=a_comments,
            avg_channel_views=avg_views,
            cpm=cpm,
        )

        if recommendation:
            async with async_session_maker() as session:
                analytics_to_save = await session.get(PostAnalytics, analytics_id)
                if analytics_to_save:
                    analytics_to_save.ai_recommendation = recommendation
                    await session.commit()

            total_engage = a_reactions + a_forwards + a_saves + a_comments
            er = round(total_engage / a_views * 100, 2) if a_views > 0 else 0

            text = (
                f"🤖 **AI-рекомендации для поста #{analytics_id}**\n\n"
                f"📢 Канал: {ch_name}\n"
                f"👁 Просмотры: {a_views:,} | 📈 ER: {er}%\n\n"
                f"{recommendation}"
            )
        else:
            text = "⚠️ Не удалось получить AI-рекомендацию. Проверьте настройки Claude API."

        await safe_edit_message(
            callback.message, text,
            get_post_analytics_actions_keyboard(analytics_id, has_ai=bool(recommendation))
        )
    except Exception as e:
        logger.error(f"Error in pa_ai_recommend: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "autopost_ai_recommend")
async def autopost_ai_recommend_overview(callback: CallbackQuery):
    """Обзор AI-рекомендаций по всем постам с аналитикой"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        async with async_session_maker() as session:
            # Топ-5 постов по Engagement Rate
            result = await session.execute(
                select(PostAnalytics)
                .where(PostAnalytics.views > 0)
                .order_by(PostAnalytics.recorded_at.desc())
                .limit(10)
            )
            analytics_list = result.scalars().all()

        if not analytics_list:
            await safe_edit_message(
                callback.message,
                "🤖 **AI-рекомендации**\n\nЕщё нет данных для анализа.\nВнесите метрики для опубликованных постов.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Опубликованные посты", callback_data="autopost_posted")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")]
                ])
            )
            return

        # Рассчитываем ER для каждого поста
        ranked = []
        for a in analytics_list:
            total_engage = a.reactions + a.forwards + a.saves + a.comments
            er = round(total_engage / a.views * 100, 2) if a.views > 0 else 0
            ranked.append((a, er))
        ranked.sort(key=lambda x: x[1], reverse=True)

        text = "🤖 **AI-рекомендации по постам**\n\n📈 Топ постов по вовлечённости:\n\n"
        buttons = []
        for i, (a, er) in enumerate(ranked[:5], 1):
            has_rec = "✅" if a.ai_recommendation else "💡"
            text += f"{i}. #{a.id} — 👁{a.views:,} | ER: {er}% {has_rec}\n"
            buttons.append([InlineKeyboardButton(
                text=f"#{a.id} — ER {er}% {'(есть рек.)' if a.ai_recommendation else '→ получить'}",
                callback_data=f"pa_ai:{a.id}" if not a.ai_recommendation else f"pa_view:{a.id}"
            )])

        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")])
        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in autopost_ai_recommend_overview: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== СОЗДАНИЕ ПОСТА (АВТОПОСТИНГ) ====================

@router.callback_query(F.data == "autopost_create")
async def autopost_create_start(callback: CallbackQuery, state: FSMContext):
    """Шаг 1 — выбор канала для нового поста"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Channel).where(Channel.is_active == True).order_by(Channel.name)
            )
            channels = result.scalars().all()
            channels_data = [{"id": ch.id, "name": ch.name} for ch in channels]

        if not channels_data:
            await safe_edit_message(
                callback.message,
                "❌ Нет активных каналов для публикации.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_autoposting")]
                ])
            )
            return

        buttons = []
        for ch in channels_data:
            buttons.append([InlineKeyboardButton(
                text=f"📢 {ch['name']}",
                callback_data=f"autopost_create_ch:{ch['id']}"
            )])
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="adm_autoposting")])

        await safe_edit_message(
            callback.message,
            "➕ **Создание поста**\n\nШаг 1/4 — Выберите канал для публикации:",
            InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await state.set_state(AdminCreatePostStates.selecting_channel)
    except Exception as e:
        logger.error(f"Error in autopost_create_start: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("autopost_create_ch:"), AdminCreatePostStates.selecting_channel)
async def autopost_create_channel(callback: CallbackQuery, state: FSMContext):
    """Шаг 2 — выбор даты публикации через календарь"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    channel_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await callback.answer("❌ Канал не найден", show_alert=True)
                return
            channel_name = channel.name

        await state.update_data(create_channel_id=channel_id, create_channel_name=channel_name)

        today = date_type.today()
        await safe_edit_message(
            callback.message,
            f"➕ **Создание поста**\n\n"
            f"📢 Канал: **{channel_name}**\n\n"
            f"Шаг 2/4 — Выберите дату публикации:",
            get_free_calendar_keyboard(today.year, today.month)
        )
        await state.set_state(AdminCreatePostStates.selecting_date)
    except Exception as e:
        logger.error(f"Error in autopost_create_channel: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("autopost_cal_nav:"), AdminCreatePostStates.selecting_date)
async def autopost_cal_nav(callback: CallbackQuery, state: FSMContext):
    """Навигация по месяцам в календаре"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        _, year_str, month_str = callback.data.split(":")
        year, month = int(year_str), int(month_str)
        data = await state.get_data()
        channel_name = data.get("create_channel_name", "—")

        await safe_edit_message(
            callback.message,
            f"➕ **Создание поста**\n\n"
            f"📢 Канал: **{channel_name}**\n\n"
            f"Шаг 2/4 — Выберите дату публикации:",
            get_free_calendar_keyboard(year, month)
        )
    except Exception:
        logger.error(f"Error in autopost_cal_nav: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("autopost_cal_date:"), AdminCreatePostStates.selecting_date)
async def autopost_cal_date(callback: CallbackQuery, state: FSMContext):
    """Дата выбрана — показываем выбор времени"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        date_iso = callback.data.split(":", 1)[1]
        selected_date = date_type.fromisoformat(date_iso)
        data = await state.get_data()
        channel_name = data.get("create_channel_name", "—")

        await state.update_data(create_selected_date=date_iso)

        await safe_edit_message(
            callback.message,
            f"➕ **Создание поста**\n\n"
            f"📢 Канал: **{channel_name}**\n"
            f"📅 Дата: **{selected_date.strftime('%d.%m.%Y')}**\n\n"
            f"Шаг 2/4 — Выберите время публикации:",
            get_time_picker_keyboard(date_iso)
        )
        await state.set_state(AdminCreatePostStates.entering_time)
    except Exception:
        logger.error(f"Error in autopost_cal_date: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "autopost_cal_back", AdminCreatePostStates.entering_time)
async def autopost_cal_back(callback: CallbackQuery, state: FSMContext):
    """Возврат из выбора времени обратно к календарю"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        data = await state.get_data()
        channel_name = data.get("create_channel_name", "—")
        date_iso = data.get("create_selected_date", "")
        if date_iso:
            d = date_type.fromisoformat(date_iso)
            year, month = d.year, d.month
        else:
            today = date_type.today()
            year, month = today.year, today.month

        await safe_edit_message(
            callback.message,
            f"➕ **Создание поста**\n\n"
            f"📢 Канал: **{channel_name}**\n\n"
            f"Шаг 2/4 — Выберите дату публикации:",
            get_free_calendar_keyboard(year, month)
        )
        await state.set_state(AdminCreatePostStates.selecting_date)
    except Exception:
        logger.error(f"Error in autopost_cal_back: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("autopost_time:"), AdminCreatePostStates.entering_time)
async def autopost_select_time(callback: CallbackQuery, state: FSMContext):
    """Время выбрано кнопкой — переходим к выбору часов удаления"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        parts = callback.data.split(":")  # autopost_time:YYYY-MM-DD:HHMM
        date_iso = parts[1]
        cb_time = parts[2]  # e.g. "1400"
        time_str = f"{cb_time[:2]}:{cb_time[2:]}"  # "14:00"
        # User enters local time (LOCAL_TZ_OFFSET from UTC); convert to UTC for storage
        scheduled_time_msk = datetime.fromisoformat(f"{date_iso}T{time_str}")
        scheduled_time = scheduled_time_msk - LOCAL_TZ_OFFSET

        if scheduled_time < datetime.utcnow():
            await callback.answer("❌ Выбранное время уже прошло. Выберите другое.", show_alert=True)
            return

        await state.update_data(create_scheduled_time=scheduled_time.isoformat())

        data = await state.get_data()
        channel_name = data.get("create_channel_name", "—")

        await safe_edit_message(
            callback.message,
            f"✅ Дата и время: **{scheduled_time_msk.strftime('%d.%m.%Y %H:%M')} {LOCAL_TZ_LABEL}**\n\n"
            f"📢 Канал: **{channel_name}**\n\n"
            f"Шаг 3/4 — Через сколько часов удалить пост?",
            InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="24ч", callback_data="autopost_del:24"),
                    InlineKeyboardButton(text="48ч", callback_data="autopost_del:48"),
                    InlineKeyboardButton(text="Не удалять", callback_data="autopost_del:0"),
                ],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_autoposting")]
            ])
        )
        await state.set_state(AdminCreatePostStates.entering_delete_hours)
    except Exception:
        logger.error(f"Error in autopost_select_time: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.message(AdminCreatePostStates.entering_time)
async def autopost_enter_time_text(message: Message, state: FSMContext):
    """Ввод времени текстом (ЧЧ:ММ) как запасной вариант"""
    raw = (message.text or "").strip()
    try:
        time_obj = datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите время в формате `ЧЧ:ММ`, например `14:00`\n"
            "или выберите из кнопок выше.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    data = await state.get_data()
    date_iso = data.get("create_selected_date", "")
    if not date_iso:
        await message.answer("❌ Дата не выбрана. Начните заново.")
        await state.clear()
        return

    scheduled_time_msk = datetime.combine(date_type.fromisoformat(date_iso), time_obj)
    scheduled_time = scheduled_time_msk - LOCAL_TZ_OFFSET  # convert local → UTC

    if scheduled_time < datetime.utcnow():
        await message.answer("❌ Дата публикации должна быть в будущем. Введите другое время.")
        return

    await state.update_data(create_scheduled_time=scheduled_time.isoformat())
    channel_name = data.get("create_channel_name", "—")

    await message.answer(
        f"✅ Дата и время: **{scheduled_time_msk.strftime('%d.%m.%Y %H:%M')} {LOCAL_TZ_LABEL}**\n\n"
        f"📢 Канал: **{channel_name}**\n\n"
        f"Шаг 3/4 — Через сколько часов удалить пост?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="24ч", callback_data="autopost_del:24"),
                InlineKeyboardButton(text="48ч", callback_data="autopost_del:48"),
                InlineKeyboardButton(text="Не удалять", callback_data="autopost_del:0"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_autoposting")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminCreatePostStates.entering_delete_hours)


@router.callback_query(F.data.startswith("autopost_del:"), AdminCreatePostStates.entering_delete_hours)
async def autopost_create_delete_hours_btn(callback: CallbackQuery, state: FSMContext):
    """Шаг 3 (кнопкой) — выбрано время удаления"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    delete_hours = int(callback.data.split(":")[1])
    await state.update_data(create_delete_hours=delete_hours)

    await safe_edit_message(
        callback.message,
        f"✅ Удалить через: **{'не удалять' if delete_hours == 0 else f'{delete_hours}ч'}**\n\n"
        f"Шаг 4/4 — Отправьте рекламный контент поста\n"
        f"(текст, или текст с фото/видео/документом):",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_autoposting")]
        ])
    )
    await state.set_state(AdminCreatePostStates.entering_content)


@router.message(AdminCreatePostStates.entering_delete_hours)
async def autopost_create_delete_hours_text(message: Message, state: FSMContext):
    """Шаг 3 (текстом) — ввод времени удаления"""
    raw = (message.text or "").strip()
    try:
        delete_hours = int(raw)
        if delete_hours < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число ≥ 0 (например: 24).")
        return

    await state.update_data(create_delete_hours=delete_hours)

    await message.answer(
        f"✅ Удалить через: **{'не удалять' if delete_hours == 0 else f'{delete_hours}ч'}**\n\n"
        f"Шаг 4/4 — Отправьте рекламный контент поста\n"
        f"(текст, или текст с фото/видео/документом):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_autoposting")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminCreatePostStates.entering_content)


@router.message(AdminCreatePostStates.entering_content)
async def autopost_create_content(message: Message, state: FSMContext):
    """Шаг 4 — получение контента, показ превью и подтверждение"""
    content_text = message.text or message.caption or ""
    file_id = None
    file_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"

    if not content_text and not file_id:
        await message.answer(
            "❌ Отправьте текст или медиафайл (фото/видео/документ).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_autoposting")]
            ])
        )
        return

    await state.update_data(
        create_content=content_text,
        create_file_id=file_id,
        create_file_type=file_type
    )

    data = await state.get_data()
    channel_name = data.get("create_channel_name", "—")
    scheduled_time_iso = data.get("create_scheduled_time", "")
    delete_hours = data.get("create_delete_hours", 24)

    try:
        scheduled_dt = datetime.fromisoformat(scheduled_time_iso)
        sched_str = (scheduled_dt + LOCAL_TZ_OFFSET).strftime("%d.%m.%Y %H:%M") + f" {LOCAL_TZ_LABEL}"
    except Exception:
        sched_str = scheduled_time_iso

    delete_str = "не удалять" if delete_hours == 0 else f"через {delete_hours}ч"

    preview = (
        f"📋 **Подтверждение создания поста**\n\n"
        f"📢 Канал: **{channel_name}**\n"
        f"📅 Время публикации: **{sched_str}**\n"
        f"🗑 Удалить: **{delete_str}**\n"
    )
    if content_text:
        preview += f"\n📝 Текст:\n{content_text[:400]}{'...' if len(content_text) > 400 else ''}\n"
    if file_id:
        preview += f"\n📎 Медиафайл: {file_type}\n"

    preview += "\nСоздать пост?"

    await message.answer(
        preview,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Создать", callback_data="autopost_create_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="adm_autoposting")
            ]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminCreatePostStates.confirming)


@router.callback_query(F.data == "autopost_create_confirm", AdminCreatePostStates.confirming)
async def autopost_create_confirm(callback: CallbackQuery, state: FSMContext):
    """Финальное сохранение поста в БД"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    data = await state.get_data()

    try:
        scheduled_time = datetime.fromisoformat(data["create_scheduled_time"])

        async with async_session_maker() as session:
            post = ScheduledPost(
                channel_id=data["create_channel_id"],
                content=data.get("create_content") or None,
                file_id=data.get("create_file_id"),
                file_type=data.get("create_file_type"),
                scheduled_time=scheduled_time,
                delete_after_hours=data.get("create_delete_hours", 24),
                status="pending",
                created_by=callback.from_user.id
            )
            session.add(post)
            await session.commit()
            post_id = post.id

        await state.clear()

        channel_name = data.get("create_channel_name", "—")
        delete_hours = data.get("create_delete_hours", 24)
        delete_str = "не удалять" if delete_hours == 0 else f"через {delete_hours}ч"

        await safe_edit_message(
            callback.message,
            f"✅ **Пост #{post_id} создан!**\n\n"
            f"📢 Канал: **{channel_name}**\n"
            f"📅 Публикация: **{(scheduled_time + LOCAL_TZ_OFFSET).strftime('%d.%m.%Y %H:%M')} {LOCAL_TZ_LABEL}**\n"
            f"🗑 Удаление: **{delete_str}**\n\n"
            f"Пост поставлен в очередь автопостинга.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Запланированные", callback_data="autopost_pending")],
                [InlineKeyboardButton(text="◀️ Автопостинг", callback_data="adm_autoposting")]
            ])
        )
    except Exception as e:
        await state.clear()
        logger.error(f"Error in autopost_create_confirm: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка при создании поста:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


# ==================== НАСТРОЙКИ ====================

@router.callback_query(F.data == "adm_settings")
async def adm_settings(callback: CallbackQuery):
    """Настройки бота"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    chat_id = await get_manager_group_chat_id()
    chat_status = f"🟢 {chat_id}" if chat_id else "🔴 Не задан"

    text = (
        "⚙️ **Настройки**\n\n"
        f"📝 Автопостинг: {'🟢' if AUTOPOST_ENABLED else '🔴'}\n"
        f"🤖 Claude API: {'🟢' if CLAUDE_API_KEY else '🔴'}\n"
        f"📊 Telemetr API: {'🟢' if TELEMETR_API_TOKEN else '🔴'}\n"
        f"🔵 Max Bot: {'🟢 Подключён' if MAX_BOT_TOKEN else '🔴 Не подключён'}\n"
        f"💬 Чат менеджеров: {chat_status}\n"
        f"👤 Админы: {len(ADMIN_IDS)}"
    )

    buttons = [
        [InlineKeyboardButton(text="💬 Чат менеджеров", callback_data="adm_manager_chat_settings")],
        [InlineKeyboardButton(text="🔵 Настройки Max Bot", callback_data="adm_max_settings")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
    ]

    await safe_edit_message(
        callback.message, text,
        InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data == "adm_manager_chat_settings")
async def adm_manager_chat_settings(callback: CallbackQuery):
    """Информация о чате менеджеров и кнопка для изменения."""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    chat_id = await get_manager_group_chat_id()

    if chat_id:
        status = (
            f"🟢 **Чат менеджеров подключён**\n\n"
            f"ID чата: `{chat_id}`\n\n"
            f"Бот будет отправлять статистику канала в этот чат при каждом "
            f"подтверждении оплаты и автопубликации поста."
        )
    else:
        status = (
            "🔴 **Чат менеджеров не настроен**\n\n"
            "Статистика каналов при публикации не отправляется.\n\n"
            "**Как подключить:**\n"
            "1️⃣ Создайте группу в Telegram\n"
            "2️⃣ Добавьте этого бота в группу как администратора\n"
            "3️⃣ Бот автоматически пришлёт вам ID группы\n"
            "4️⃣ Нажмите «✏️ Изменить» и введите полученный ID"
        )

    await safe_edit_message(
        callback.message,
        status,
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить ID чата", callback_data="adm_manager_chat_input")],
            [InlineKeyboardButton(text="🗑 Сбросить", callback_data="adm_manager_chat_clear")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_settings")],
        ])
    )


@router.callback_query(F.data == "adm_manager_chat_input")
async def adm_manager_chat_input(callback: CallbackQuery, state: FSMContext):
    """Запросить новый ID чата менеджеров."""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    await safe_edit_message(
        callback.message,
        "💬 **Введите ID чата менеджеров**\n\n"
        "Пример: `-1001234567890`\n\n"
        "ID должен быть отрицательным числом (для групп/супергрупп).\n"
        "Если вы добавили бота в группу — он уже прислал вам ID.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_manager_chat_settings")]
        ])
    )
    await state.set_state(AdminSettingsStates.waiting_manager_chat_id)


@router.message(AdminSettingsStates.waiting_manager_chat_id)
async def adm_manager_chat_receive(message: Message, state: FSMContext):
    """Сохранить новый ID чата менеджеров."""
    raw = (message.text or "").strip()
    try:
        chat_id = int(raw)
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введите числовой ID чата, например `-1001234567890`.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await set_setting(MANAGER_GROUP_CHAT_ID_KEY, str(chat_id), updated_by=message.from_user.id)
    await state.clear()

    await message.answer(
        f"✅ **Чат менеджеров обновлён!**\n\nID: `{chat_id}`\n\n"
        f"Теперь бот будет отправлять статистику канала в этот чат при каждой публикации.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ К настройкам", callback_data="adm_settings")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data == "adm_manager_chat_clear")
async def adm_manager_chat_clear(callback: CallbackQuery):
    """Сбросить ID чата менеджеров."""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    await set_setting(MANAGER_GROUP_CHAT_ID_KEY, None, updated_by=callback.from_user.id)

    await safe_edit_message(
        callback.message,
        "🗑 **Чат менеджеров сброшен.**\n\nСтатистика больше не будет отправляться в группу.",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К настройкам", callback_data="adm_settings")]
        ])
    )


@router.my_chat_member()
async def on_bot_added_to_group(event: ChatMemberUpdated, bot: Bot):
    """Когда бот добавляется в группу/супергруппу — сообщить администраторам ID чата."""
    new_status = event.new_chat_member.status
    chat = event.chat

    # Только вхождение в группы/супергруппы
    if chat.type not in ("group", "supergroup"):
        return
    if new_status not in ("member", "administrator"):
        return

    chat_id = chat.id
    chat_title = chat.title or "Без названия"

    notify_text = (
        f"🤖 Бот добавлен в группу!\n\n"
        f"📛 Название: {chat_title}\n"
        f"🆔 ID чата: `{chat_id}`\n\n"
        f"Скопируйте ID и вставьте его в:\n"
        f"⚙️ Настройки → 💬 Чат менеджеров → ✏️ Изменить ID чата"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, notify_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


@router.callback_query(F.data == "adm_max_settings")
async def adm_max_settings(callback: CallbackQuery):
    """Информация о подключении Max Bot"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    if MAX_BOT_TOKEN:
        status_text = "🟢 **Max Bot подключён**\n\nБот в сети Max активен и принимает пользователей."
    else:
        status_text = (
            "🔴 **Max Bot не подключён**\n\n"
            "Для подключения бота в сеть Max:\n"
            "1. Перейдите на **dev.max.ru** и создайте бота\n"
            "2. Получите токен бота\n"
            "3. Установите переменную окружения:\n"
            "`MAX_BOT_TOKEN=ваш_токен`\n"
            "4. Перезапустите приложение\n\n"
            "После подключения пользователи Max смогут:\n"
            "• Просматривать каталог каналов\n"
            "• Бронировать рекламные слоты\n"
            "• Управлять своим профилем менеджера"
        )
    
    await safe_edit_message(
        callback.message, status_text,
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_settings")]
        ])
    )


# ==================== ДИАГНОСТИКА И AI-УЛУЧШЕНИЯ ====================

@router.callback_query(F.data == "adm_diagnostics")
async def adm_diagnostics(callback: CallbackQuery):
    """Самодиагностика бота — проверка всех компонентов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    # Показываем временное сообщение о проверке
    await safe_edit_message(
        callback.message,
        "🔧 **Диагностика**\n\n⏳ Проверяю компоненты...",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
        ])
    )

    try:
        results = await run_diagnostics()

        db_icon, db_msg = results.get("db", ("❓", "Нет данных"))
        claude_icon, claude_msg = results.get("claude", ("❓", "Нет данных"))
        telemetr_icon, telemetr_msg = results.get("telemetr", ("❓", "Нет данных"))
        queue = results.get("queue")

        text = (
            "🔧 **Диагностика бота**\n\n"
            "**Компоненты:**\n"
            f"{db_icon} {db_msg}\n"
            f"{claude_icon} {claude_msg}\n"
            f"{telemetr_icon} {telemetr_msg}\n"
        )

        if queue is not None:
            text += "\n**Очередь и задачи:**\n"
            text += f"📋 Постов в очереди: **{queue['pending_posts']}**\n"

            if queue["overdue_posts"] > 0:
                text += f"⚠️ Просроченных постов: **{queue['overdue_posts']}** — требуют внимания!\n"

            if queue["moderation_posts"] > 0:
                text += f"🔍 На модерации: **{queue['moderation_posts']}**\n"

            if queue["pending_payments"] > 0:
                text += f"💳 Оплат на проверке: **{queue['pending_payments']}** — ожидают подтверждения!\n"

            if queue["overdue_posts"] == 0 and queue["pending_payments"] == 0:
                text += "✅ Все задачи в норме\n"
        else:
            text += "\n⚠️ Не удалось проверить очередь.\n"

        text += f"\n🕐 Проверено: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC"

        buttons = [
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_diagnostics")],
            [InlineKeyboardButton(text="🔬 Глубокая диагностика", callback_data="adm_deep_diagnostics")],
            [InlineKeyboardButton(text="🤖 AI-улучшения", callback_data="adm_ai_improve")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
        ]

        await safe_edit_message(
            callback.message, text,
            InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    except Exception as e:
        logger.error(f"Error in adm_diagnostics: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка диагностики", show_alert=True)


@router.callback_query(F.data == "adm_ai_improve")
async def adm_ai_improve(callback: CallbackQuery):
    """AI-анализ метрик бота и рекомендации по улучшению"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    # Показываем временное сообщение
    await safe_edit_message(
        callback.message,
        "🤖 **AI-улучшения**\n\n⏳ Анализирую метрики и формирую рекомендации...",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
        ])
    )

    try:
        metrics = await gather_business_metrics()
        suggestion = await get_improvement_suggestions(metrics)

        if metrics:
            change = metrics.get("revenue_change_pct")
            change_str = ""
            if change is not None:
                direction = "▲" if change >= 0 else "▼"
                change_str = f" ({direction}{abs(change):.1f}%)"

            summary = (
                f"📊 **Текущие показатели:**\n"
                f"• Выручка за месяц: **{metrics.get('revenue_month', 0):,.0f}₽**{change_str}\n"
                f"• Конверсия: **{metrics.get('conversion_rate_pct', 0):.1f}%**  "
                f"| Отмены: **{metrics.get('cancel_rate_pct', 0):.1f}%**\n"
                f"• Новых клиентов: **{metrics.get('new_clients_month', 0)}** за месяц\n"
                f"• Активных менеджеров: **{metrics.get('active_managers', 0)}**\n\n"
            )
        else:
            summary = ""

        if suggestion:
            text = f"🤖 **AI-рекомендации по улучшению**\n\n{summary}{suggestion}"
        else:
            text = (
                f"🤖 **AI-рекомендации по улучшению**\n\n{summary}"
                "⚠️ Не удалось получить AI-рекомендации. Проверьте настройки Claude API."
            )

        buttons = [
            [InlineKeyboardButton(text="🔄 Обновить анализ", callback_data="adm_ai_improve")],
            [InlineKeyboardButton(text="🔧 Диагностика", callback_data="adm_diagnostics")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
        ]

        await safe_edit_message(
            callback.message, text,
            InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    except Exception as e:
        logger.error(f"Error in adm_ai_improve: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "adm_deep_diagnostics")
async def adm_deep_diagnostics(callback: CallbackQuery):
    """Углублённая диагностика всех разделов и кнопок бота"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    await safe_edit_message(
        callback.message,
        "🔬 **Глубокая диагностика**\n\n⏳ Проверяю все разделы и компоненты...\n_(это может занять до 30 секунд)_",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_diagnostics")]
        ])
    )

    try:
        report = await run_deep_diagnostics()
        total, ok, warn, error = report["summary"]

        if error == 0 and warn == 0:
            status_line = "✅ Всё в порядке"
        elif error == 0:
            status_line = f"🟡 Предупреждений: {warn}"
        else:
            status_line = f"🔴 Ошибок: {error} | Предупреждений: {warn}"

        lines = [
            "🔬 **Глубокая диагностика бота**\n",
            f"📊 Итог: {status_line} (проверок: {total}, ОК: {ok})\n",
        ]

        # Конфиг
        lines.append("\n**⚙️ Конфигурация:**")
        for icon, msg in report["config"].values():
            lines.append(f"{icon} {_md_escape(msg)}")

        # Таблицы
        lines.append("\n**🗄 Таблицы БД:**")
        for icon, msg in report["db_tables"].values():
            lines.append(f"{icon} {_md_escape(msg)}")

        # Активные данные
        lines.append("\n**📋 Активные данные:**")
        for icon, msg in report["active_data"].values():
            lines.append(f"{icon} {_md_escape(msg)}")

        # Сервисы
        lines.append("\n**🔧 Сервисы метрик:**")
        for icon, msg in report["services"].values():
            lines.append(f"{icon} {_md_escape(msg)}")

        # Внешние API
        lines.append("\n**🌐 Внешние API:**")
        for icon, msg in report["apis"].values():
            lines.append(f"{icon} {_md_escape(msg)}")

        # Разделы
        lines.append("\n**📂 Разделы и кнопки:**")
        for sect, (icon, msg) in report["sections"].items():
            lines.append(f"{icon} {_md_escape(sect)}: {_md_escape(msg)}")

        lines.append(f"\n🕐 Проверено: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC")

        text = "\n".join(lines)

        # Telegram ограничивает длину сообщений ~4096 символами
        _MAX_REPORT_LEN = 3800
        if len(text) > _MAX_REPORT_LEN:
            text = text[:_MAX_REPORT_LEN] + "\n\n_(отчёт обрезан — слишком длинный)_"

        buttons = [
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_deep_diagnostics")],
            [InlineKeyboardButton(text="🔧 Быстрая диагностика", callback_data="adm_diagnostics")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")],
        ]

        await safe_edit_message(
            callback.message, text,
            InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    except Exception as e:
        logger.error(f"Error in adm_deep_diagnostics: {traceback.format_exc()}")
        await safe_edit_message(
            callback.message,
            f"❌ **Ошибка глубокой диагностики**\n\n`{str(e)[:200]}`",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_diagnostics")]
            ])
        )


# ==================== ПРОСМОТР ЗАКАЗА ====================

@router.callback_query(F.data.startswith("adm_order:"))
async def adm_view_order(callback: CallbackQuery):
    """Просмотр заказа и подтверждение оплаты"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        order_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            order = await session.get(Order, order_id)
            if not order:
                await callback.message.answer("❌ Заказ не найден")
                return

            client = await session.get(Client, order.client_id)
            slot = await session.get(Slot, order.slot_id)
            channel = await session.get(Channel, slot.channel_id) if slot else None

        client_name = (client.first_name or client.username or f"ID {order.client_id}") if client else "—"
        channel_name = channel.name if channel else "—"
        slot_info = f"{slot.slot_date} {slot.slot_time.strftime('%H:%M')}" if slot else "—"

        status_map = {
            "pending": "⏳ Ожидает",
            "payment_uploaded": "💳 Оплата загружена",
            "payment_confirmed": "✅ Оплата подтверждена",
            "cancelled": "❌ Отменён",
            "posted": "📢 Опубликован",
            "completed": "✔️ Завершён",
        }
        status_text = status_map.get(order.status, order.status)

        text = (
            f"📄 **Заказ #{order_id}**\n\n"
            f"👤 Клиент: {client_name}\n"
            f"📢 Канал: {channel_name}\n"
            f"📅 Слот: {slot_info}\n"
            f"📋 Формат: {order.format_type}\n"
            f"💰 Базовая цена: {float(order.base_price):,.0f}₽\n"
        )

        if order.discount_percent and float(order.discount_percent) > 0:
            saved = float(order.base_price) - float(order.final_price)
            promo_label = f" (промокод: {order.promo_code})" if order.promo_code else " (лояльность)"
            text += (
                f"🏷 Скидка{promo_label}: **{float(order.discount_percent):.0f}%** (−{saved:,.0f}₽)\n"
                f"💵 Итого: **{float(order.final_price):,.0f}₽**\n"
            )
        else:
            text += f"💵 Итого: **{float(order.final_price):,.0f}₽**\n"

        text += f"📊 Статус: {status_text}\n"

        if order.ad_content:
            text += f"\n📝 Текст:\n{order.ad_content[:300]}{'...' if len(order.ad_content) > 300 else ''}\n"

        buttons = []
        if order.status == "payment_uploaded":
            buttons.append([
                InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"adm_confirm_payment:{order_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject_payment:{order_id}")
            ])
        if order.status == "payment_confirmed":
            buttons.append([
                InlineKeyboardButton(text="📢 Отметить как опубликован", callback_data=f"adm_mark_posted:{order_id}")
            ])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_payments")])

        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_view_order: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_confirm_payment:"))
async def adm_confirm_payment(callback: CallbackQuery, bot: Bot):
    """Подтвердить оплату заказа"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        order_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            order = await session.get(Order, order_id)
            if not order:
                await callback.answer("❌ Заказ не найден", show_alert=True)
                return

            order.status = "payment_confirmed"
            order.paid_at = datetime.utcnow()

            # Начисляем комиссию менеджеру
            manager_telegram_id = None
            if order.manager_id:
                manager = await session.get(Manager, order.manager_id)
                if manager and manager.is_active:
                    commission = order.final_price * manager.commission_rate / 100
                    manager.total_sales = (manager.total_sales or 0) + 1
                    manager.total_revenue = (manager.total_revenue or Decimal("0")) + order.final_price
                    manager.balance = (manager.balance or Decimal("0")) + commission
                    manager.total_earned = (manager.total_earned or Decimal("0")) + commission
                    manager_telegram_id = manager.telegram_id

            await session.commit()

            client = await session.get(Client, order.client_id)
            client_telegram_id = client.telegram_id if client else None

            # Получаем канал для уведомления в чат менеджеров
            channel_for_notify = None
            if order.slot_id:
                slot = await session.get(Slot, order.slot_id)
                if slot:
                    channel_for_notify = await session.get(Channel, slot.channel_id)

        # Начисляем XP менеджеру через gamification
        if order.manager_id:
            try:
                await gamification_service.process_sale(order.manager_id, float(order.final_price))
            except Exception as e:
                logger.warning(f"Gamification processing failed for order {order_id}: {e}")

        await callback.answer("✅ Оплата подтверждена!", show_alert=True)

        if client_telegram_id:
            try:
                await bot.send_message(
                    client_telegram_id,
                    f"✅ **Оплата по заказу #{order_id} подтверждена!**\n\n"
                    f"Ваш пост будет опубликован в указанное время.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        if manager_telegram_id:
            try:
                await bot.send_message(
                    manager_telegram_id,
                    f"💰 **Продажа засчитана!**\n\n"
                    f"Заказ #{order_id} оплачен.\n"
                    f"Сумма заказа: **{float(order.final_price):,.0f}₽**",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        # Отправляем статистику канала в чат менеджеров
        if channel_for_notify:
            await _notify_manager_group(bot, channel_for_notify, order_id)

        await safe_edit_message(
            callback.message,
            f"✅ **Оплата по заказу #{order_id} подтверждена**",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К оплатам", callback_data="adm_payments")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_confirm_payment: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_reject_payment:"))
async def adm_reject_payment(callback: CallbackQuery, bot: Bot):
    """Отклонить оплату заказа"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        order_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            order = await session.get(Order, order_id)
            if not order:
                await callback.answer("❌ Заказ не найден", show_alert=True)
                return

            order.status = "cancelled"
            await session.commit()

            client = await session.get(Client, order.client_id)
            client_telegram_id = client.telegram_id if client else None

        await callback.answer("❌ Оплата отклонена", show_alert=True)

        if client_telegram_id:
            try:
                await bot.send_message(
                    client_telegram_id,
                    f"❌ **Оплата по заказу #{order_id} не подтверждена.**\n\n"
                    f"Пожалуйста, свяжитесь с администратором.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        await safe_edit_message(
            callback.message,
            f"❌ **Оплата по заказу #{order_id} отклонена**",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К оплатам", callback_data="adm_payments")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_reject_payment: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_mark_posted:"))
async def adm_mark_posted(callback: CallbackQuery, bot: Bot):
    """Отметить заказ как опубликованный"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        order_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            order = await session.get(Order, order_id)
            if not order:
                await callback.answer("❌ Заказ не найден", show_alert=True)
                return

            if order.status != "payment_confirmed":
                await callback.answer("❌ Заказ не в статусе «оплачен»", show_alert=True)
                return

            order.status = "posted"
            order.posted_at = datetime.utcnow()

            # Помечаем слот как забронированный
            if order.slot_id:
                slot = await session.get(Slot, order.slot_id)
                if slot:
                    slot.status = "booked"

            await session.commit()

            client = await session.get(Client, order.client_id)
            client_telegram_id = client.telegram_id if client else None

        await callback.answer("📢 Заказ отмечен как опубликованный!", show_alert=True)

        if client_telegram_id:
            try:
                await bot.send_message(
                    client_telegram_id,
                    f"📢 **Ваш пост по заказу #{order_id} опубликован!**\n\n"
                    f"Спасибо за использование нашего сервиса.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

        await safe_edit_message(
            callback.message,
            f"📢 **Заказ #{order_id} отмечен как опубликованный**",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К оплатам", callback_data="adm_payments")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_mark_posted: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== ПРОСМОТР МЕНЕДЖЕРА ====================

@router.callback_query(F.data.startswith("adm_mgr:"))
async def adm_view_manager(callback: CallbackQuery):
    """Просмотр и управление менеджером"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        manager_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            manager = await session.get(Manager, manager_id)
            if not manager:
                await callback.message.answer("❌ Менеджер не найден")
                return

            level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])

        name = manager.first_name or manager.username or f"ID {manager.telegram_id}"
        status = "✅ Активен" if manager.is_active else "❌ Неактивен"

        text = (
            f"👤 **{name}**\n\n"
            f"{level_info['emoji']} Уровень: **{manager.level} — {level_info['name']}**\n"
            f"⭐ Опыт: **{manager.experience_points} XP**\n"
            f"💰 Баланс: **{float(manager.balance):,.0f}₽**\n"
            f"📦 Продаж: **{manager.total_sales}**\n"
            f"💵 Выручка: **{float(manager.total_revenue):,.0f}₽**\n"
            f"🎓 Комиссия: **{float(manager.commission_rate):.0f}%**\n"
            f"📊 Статус: {status}\n"
        )

        buttons = []

        if manager.level < 5:
            buttons.append([InlineKeyboardButton(
                text=f"⬆️ Повысить до {manager.level + 1} ур.",
                callback_data=f"adm_mgr_promote:{manager_id}"
            )])
        if manager.level > 1:
            buttons.append([InlineKeyboardButton(
                text=f"⬇️ Понизить до {manager.level - 1} ур.",
                callback_data=f"adm_mgr_demote:{manager_id}"
            )])

        toggle_text = "❌ Деактивировать" if manager.is_active else "✅ Активировать"
        buttons.append([InlineKeyboardButton(
            text=toggle_text,
            callback_data=f"adm_mgr_toggle:{manager_id}"
        )])
        buttons.append([InlineKeyboardButton(
            text="🎯 Установить комиссию",
            callback_data=f"adm_mgr_set_commission:{manager_id}"
        )])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_managers")])

        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_view_manager: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_mgr_promote:"))
async def adm_promote_manager(callback: CallbackQuery):
    """Повысить уровень менеджера"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        manager_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            manager = await session.get(Manager, manager_id)
            if not manager or manager.level >= 5:
                await callback.answer("❌ Невозможно повысить", show_alert=True)
                return

            manager.level += 1
            level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
            manager.commission_rate = level_info["commission"]
            await session.commit()

        await callback.answer(f"⬆️ Повышен до уровня {manager.level}!", show_alert=True)
        await adm_view_manager(callback)
    except Exception as e:
        logger.error(f"Error in adm_promote_manager: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_mgr_demote:"))
async def adm_demote_manager(callback: CallbackQuery):
    """Понизить уровень менеджера"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        manager_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            manager = await session.get(Manager, manager_id)
            if not manager or manager.level <= 1:
                await callback.answer("❌ Невозможно понизить", show_alert=True)
                return

            manager.level -= 1
            level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
            manager.commission_rate = level_info["commission"]
            await session.commit()

        await callback.answer(f"⬇️ Понижен до уровня {manager.level}", show_alert=True)
        await adm_view_manager(callback)
    except Exception as e:
        logger.error(f"Error in adm_demote_manager: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_mgr_toggle:"))
async def adm_toggle_manager(callback: CallbackQuery):
    """Активировать/деактивировать менеджера"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        manager_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            manager = await session.get(Manager, manager_id)
            if not manager:
                await callback.answer("❌ Менеджер не найден", show_alert=True)
                return

            manager.is_active = not manager.is_active
            await session.commit()

        status = "✅ Активирован" if manager.is_active else "❌ Деактивирован"
        await callback.answer(status, show_alert=True)
        await adm_view_manager(callback)
    except Exception as e:
        logger.error(f"Error in adm_toggle_manager: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_mgr_set_commission:"))
async def adm_mgr_set_commission_start(callback: CallbackQuery, state: FSMContext):
    """Начать ввод комиссии для менеджера"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        manager_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            manager = await session.get(Manager, manager_id)
            if not manager:
                await callback.answer("❌ Менеджер не найден", show_alert=True)
                return
            current_rate = float(manager.commission_rate)

        await state.set_state(AdminManagerStates.waiting_commission_rate)
        await state.update_data(target_manager_id=manager_id)

        await safe_edit_message(
            callback.message,
            f"🎯 **Ручная установка комиссии**\n\n"
            f"Текущая комиссия менеджера: **{current_rate:.1f}%**\n\n"
            f"Введите новое значение комиссии в процентах (от 0 до 100).\n"
            f"Например: `15` или `12.5`",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_mgr:{manager_id}")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_mgr_set_commission_start: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.message(AdminManagerStates.waiting_commission_rate)
async def adm_mgr_set_commission_receive(message: Message, state: FSMContext):
    """Получить и сохранить новое значение комиссии"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return

    raw = (message.text or "").strip().replace(",", ".")
    try:
        new_rate = float(raw)
        if not (0 <= new_rate <= 100):
            raise ValueError("out of range")
    except ValueError:
        await message.answer(
            "❌ Некорректное значение. Введите число от 0 до 100.\nНапример: `15` или `12.5`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    data = await state.get_data()
    manager_id = data.get("target_manager_id")
    await state.clear()

    try:
        async with async_session_maker() as session:
            manager = await session.get(Manager, manager_id)
            if not manager:
                await message.answer("❌ Менеджер не найден")
                return

            old_rate = float(manager.commission_rate)
            manager.commission_rate = Decimal(str(new_rate))
            await session.commit()

        await message.answer(
            f"✅ **Комиссия обновлена**\n\n"
            f"Было: {old_rate:.1f}%\n"
            f"Стало: {new_rate:.1f}%",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 К менеджеру", callback_data=f"adm_mgr:{manager_id}")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_mgr_set_commission_receive: {traceback.format_exc()}")
        await message.answer("❌ Ошибка при сохранении комиссии")


# ==================== ПРОСМОТР ПОСТА НА МОДЕРАЦИИ ====================

@router.callback_query(F.data.startswith("adm_post:"))
async def adm_view_post(callback: CallbackQuery):
    """Просмотр поста на модерации"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        post_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            post = await session.get(ScheduledPost, post_id)
            if not post:
                await callback.message.answer("❌ Пост не найден")
                return

            channel = await session.get(Channel, post.channel_id)

        channel_name = channel.name if channel else "—"
        scheduled_time = (post.scheduled_time + LOCAL_TZ_OFFSET).strftime("%d.%m.%Y %H:%M") + f" {LOCAL_TZ_LABEL}" if post.scheduled_time else "—"

        text = (
            f"📄 **Пост #{post_id} на модерации**\n\n"
            f"📢 Канал: {channel_name}\n"
            f"📅 Время: {scheduled_time}\n"
            f"🗑 Удалить через: {post.delete_after_hours}ч\n\n"
        )

        if post.content:
            text += f"📝 Текст:\n{post.content[:400]}{'...' if len(post.content) > 400 else ''}\n"

        if post.file_type:
            text += f"\n📎 Медиа: {post.file_type}\n"

        buttons = [
            [
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_post_approve:{post_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_post_reject:{post_id}")
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_moderation")]
        ]

        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_view_post: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_post_approve:"))
async def adm_approve_post(callback: CallbackQuery):
    """Одобрить пост"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        post_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            post = await session.get(ScheduledPost, post_id)
            if not post:
                await callback.answer("❌ Пост не найден", show_alert=True)
                return

            post.status = "pending"
            await session.commit()

        await callback.answer("✅ Пост одобрен и поставлен в очередь!", show_alert=True)

        await safe_edit_message(
            callback.message,
            f"✅ **Пост #{post_id} одобрен**\n\nПоставлен в очередь автопостинга.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К модерации", callback_data="adm_moderation")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_approve_post: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_post_reject:"))
async def adm_reject_post(callback: CallbackQuery):
    """Отклонить пост"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        post_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            post = await session.get(ScheduledPost, post_id)
            if not post:
                await callback.answer("❌ Пост не найден", show_alert=True)
                return

            post.status = "rejected"
            await session.commit()

        await callback.answer("❌ Пост отклонён", show_alert=True)

        await safe_edit_message(
            callback.message,
            f"❌ **Пост #{post_id} отклонён**",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К модерации", callback_data="adm_moderation")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_reject_post: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== СОЗДАНИЕ СОРЕВНОВАНИЯ ====================

@router.callback_query(F.data == "adm_create_competition")
async def adm_create_competition_start(callback: CallbackQuery, state: FSMContext):
    """Начать создание соревнования"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    await safe_edit_message(
        callback.message,
        "🏆 **Создание соревнования**\n\nВведите название соревнования:",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_competitions")]
        ])
    )
    await state.set_state(AdminCompetitionStates.waiting_name)


@router.message(AdminCompetitionStates.waiting_name)
async def adm_competition_name(message: Message, state: FSMContext):
    """Получить название соревнования"""
    name = message.text.strip()
    if not name:
        await message.answer("❌ Введите название")
        return

    await state.update_data(competition_name=name)

    await message.answer(
        f"✅ Название: **{name}**\n\nВведите дату начала (ДД.ММ.ГГГГ):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_competitions")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(AdminCompetitionStates.waiting_start_date)


@router.message(AdminCompetitionStates.waiting_start_date)
async def adm_competition_start_date(message: Message, state: FSMContext):
    """Получить дату начала соревнования"""
    try:
        start_date = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату в формате ДД.ММ.ГГГГ")
        return

    await state.update_data(competition_start=start_date.isoformat())

    await message.answer(
        f"✅ Начало: **{start_date.strftime('%d.%m.%Y')}**\n\nВведите дату окончания (ДД.ММ.ГГГГ):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_competitions")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(AdminCompetitionStates.waiting_end_date)


@router.message(AdminCompetitionStates.waiting_end_date)
async def adm_competition_end_date(message: Message, state: FSMContext):
    """Получить дату окончания соревнования"""
    try:
        end_date = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату в формате ДД.ММ.ГГГГ")
        return

    data = await state.get_data()
    start_date = date_type.fromisoformat(data["competition_start"])

    if end_date <= start_date:
        await message.answer("❌ Дата окончания должна быть позже даты начала")
        return

    await state.update_data(competition_end=end_date.isoformat())

    await message.answer(
        f"✅ Окончание: **{end_date.strftime('%d.%m.%Y')}**\n\nВведите призовой фонд (₽, число):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_competitions")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(AdminCompetitionStates.waiting_prize_pool)


@router.message(AdminCompetitionStates.waiting_prize_pool)
async def adm_competition_prize_pool(message: Message, state: FSMContext):
    """Получить призовой фонд"""
    try:
        prize_pool = float(message.text.strip().replace(" ", "").replace(",", ""))
        if prize_pool < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректное число (например: 5000)")
        return

    await state.update_data(competition_prize=prize_pool)

    await message.answer(
        f"✅ Призовой фонд: **{prize_pool:,.0f}₽**\n\nВыберите метрику соревнования:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 По продажам", callback_data="comp_metric:sales"),
                InlineKeyboardButton(text="💰 По выручке", callback_data="comp_metric:revenue")
            ],
            [InlineKeyboardButton(text="⭐ По опыту (XP)", callback_data="comp_metric:xp")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_competitions")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(AdminCompetitionStates.waiting_metric)


@router.callback_query(F.data.startswith("comp_metric:"), AdminCompetitionStates.waiting_metric)
async def adm_competition_metric(callback: CallbackQuery, state: FSMContext):
    """Выбрать метрику и создать соревнование"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    metric = callback.data.split(":")[1]
    metric_names = {"sales": "продажи", "revenue": "выручка", "xp": "опыт (XP)"}
    data = await state.get_data()

    try:
        async with async_session_maker() as session:
            competition = Competition(
                name=data["competition_name"],
                start_date=date_type.fromisoformat(data["competition_start"]),
                end_date=date_type.fromisoformat(data["competition_end"]),
                prize_pool=Decimal(str(data["competition_prize"])),
                metric=metric,
                status="active"
            )
            session.add(competition)
            await session.commit()
            competition_id = competition.id

        await state.clear()

        await safe_edit_message(
            callback.message,
            f"🏆 **Соревнование создано!**\n\n"
            f"📌 {data['competition_name']}\n"
            f"📅 {date_type.fromisoformat(data['competition_start']).strftime('%d.%m.%Y')} — "
            f"{date_type.fromisoformat(data['competition_end']).strftime('%d.%m.%Y')}\n"
            f"💰 Призовой фонд: {float(data['competition_prize']):,.0f}₽\n"
            f"📊 Метрика: {metric_names.get(metric, metric)}",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К соревнованиям", callback_data="adm_competitions")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_competition_metric: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode="Markdown")
        await state.clear()


# ==================== ТЕКСТОВЫЕ КНОПКИ АДМИНА ====================

@router.message(F.text == "📢 Каналы")
async def btn_adm_channels(message: Message):
    """Текстовая кнопка — список каналов"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(Channel))
            channels = result.scalars().all()
            channels_data = [{"id": ch.id, "name": ch.name, "is_active": ch.is_active} for ch in channels]

        if channels_data:
            text = "📢 **Каналы:**\n\n"
            buttons = []
            for ch in channels_data:
                status = "✅" if ch["is_active"] else "❌"
                text += f"{status} **{ch['name']}** (ID: {ch['id']})\n"
                buttons.append([InlineKeyboardButton(text=f"⚙️ {ch['name']}", callback_data=f"adm_ch:{ch['id']}")])
            buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "📢 Каналов пока нет"
            buttons = [
                [InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
            ]

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in btn_adm_channels: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "💳 Оплаты")
async def btn_adm_payments(message: Message):
    """Текстовая кнопка — оплаты"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Order).where(Order.status == "payment_uploaded").order_by(Order.created_at.desc())
            )
            orders = result.scalars().all()
            orders_data = [{"id": o.id, "price": float(o.final_price or 0)} for o in orders[:10]]

        if orders_data:
            text = f"💳 **Оплаты: {len(orders_data)}**\n\n"
            buttons = []
            for o in orders_data:
                text += f"• #{o['id']} — {o['price']:,.0f}₽\n"
                buttons.append([InlineKeyboardButton(text=f"📄 #{o['id']}", callback_data=f"adm_order:{o['id']}")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "✅ Нет оплат на проверке"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in btn_adm_payments: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "👥 Менеджеры")
async def btn_adm_managers(message: Message):
    """Текстовая кнопка — менеджеры"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(Manager).order_by(Manager.total_sales.desc()))
            managers = result.scalars().all()
            managers_data = [{"id": m.id, "name": m.first_name or m.username or f"ID:{m.telegram_id}", "sales": m.total_sales or 0, "level": m.level} for m in managers[:20]]

        if managers_data:
            text = "👥 **Менеджеры:**\n\n"
            buttons = []
            for m in managers_data:
                text += f"• **{m['name']}** — ур.{m['level']}, {m['sales']} продаж\n"
                buttons.append([InlineKeyboardButton(text=f"👤 {m['name']}", callback_data=f"adm_mgr:{m['id']}")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "👥 Менеджеров пока нет"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in btn_adm_managers: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "📊 Статистика")
async def btn_adm_stats(message: Message):
    """Текстовая кнопка — статистика + навигация по метрикам"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return
    try:
        from keyboards.menus import get_metrics_menu
        text = await _build_crm_stats_text()
        await message.answer(text, reply_markup=get_metrics_menu(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in btn_adm_stats: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "📝 Модерация")
async def btn_adm_moderation(message: Message):
    """Текстовая кнопка — модерация"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledPost).where(ScheduledPost.status == "moderation").order_by(ScheduledPost.created_at.desc())
            )
            posts = result.scalars().all()
            posts_data = []
            for post in posts[:10]:
                channel = await session.get(Channel, post.channel_id)
                posts_data.append({"id": post.id, "channel_name": channel.name if channel else "N/A"})

        if posts_data:
            text = f"📝 **Модерация: {len(posts_data)}**\n\n"
            buttons = []
            for post in posts_data:
                text += f"• #{post['id']} — {post['channel_name']}\n"
                buttons.append([InlineKeyboardButton(text=f"📄 #{post['id']}", callback_data=f"adm_post:{post['id']}")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "✅ Нет постов на модерации"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in btn_adm_moderation: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "🏆 Лидерборд")
async def btn_adm_leaderboard(message: Message):
    """Текстовая кнопка — лидерборд (соревнования)"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.is_active == True).order_by(Manager.total_sales.desc()).limit(10)
            )
            managers = result.scalars().all()

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        if managers:
            text = "🏆 **Рейтинг менеджеров**\n\n"
            for i, m in enumerate(managers, 1):
                medal = medals.get(i, f"{i}.")
                text += f"{medal} {m.first_name or m.username or 'Менеджер'} — {m.total_sales} продаж\n"
        else:
            text = "🏆 Рейтинг пока пуст"

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏆 Соревнования", callback_data="adm_competitions")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
        ]), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in btn_adm_leaderboard: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "⚙️ Настройки")
async def btn_adm_settings(message: Message):
    """Текстовая кнопка — настройки"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return
    chat_id = await get_manager_group_chat_id()
    chat_status = f"🟢 {chat_id}" if chat_id else "🔴 Не задан"
    text = (
        "⚙️ **Настройки**\n\n"
        f"📝 Автопостинг: {'🟢' if AUTOPOST_ENABLED else '🔴'}\n"
        f"🤖 Claude API: {'🟢' if CLAUDE_API_KEY else '🔴'}\n"
        f"📊 Telemetr API: {'🟢' if TELEMETR_API_TOKEN else '🔴'}\n"
        f"🔵 Max Bot: {'🟢 Подключён' if MAX_BOT_TOKEN else '🔴 Не подключён'}\n"
        f"💬 Чат менеджеров: {chat_status}\n"
        f"👤 Админы: {len(ADMIN_IDS)}"
    )
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Чат менеджеров", callback_data="adm_manager_chat_settings")],
        [InlineKeyboardButton(text="🔵 Настройки Max Bot", callback_data="adm_max_settings")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
    ]), parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "🚪 Выйти")
async def btn_adm_logout(message: Message):
    """Текстовая кнопка — выйти из админки"""
    if message.from_user.id in authenticated_admins:
        authenticated_admins.discard(message.from_user.id)
        await message.answer("👋 Вы вышли из админ-панели")


# ==================== ПРОМОКОДЫ ====================

@router.callback_query(F.data == "adm_promo")
async def adm_promo_list(callback: CallbackQuery):
    """Список промокодов"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(PromoCode).order_by(PromoCode.created_at.desc()).limit(20)
            )
            promos = result.scalars().all()

        if not promos:
            text = "🎟 **Промокоды**\n\nПромокодов пока нет."
        else:
            text = "🎟 **Промокоды**\n\n"
            for p in promos:
                status = "🟢" if p.is_active else "🔴"
                uses = f"{p.uses_count}/{p.max_uses}" if p.max_uses else str(p.uses_count)
                exp = p.expires_at.strftime("%d.%m.%Y") if p.expires_at else "∞"
                text += f"{status} `{p.code}` — **{float(p.discount_percent):.0f}%** | использований: {uses} | до {exp}\n"

        buttons = [
            [InlineKeyboardButton(text="➕ Создать промокод", callback_data="adm_promo_create")],
        ]

        # Кнопки деактивации для активных кодов
        if promos:
            active = [p for p in promos if p.is_active]
            for p in active[:5]:
                buttons.append([InlineKeyboardButton(
                    text=f"❌ Деактивировать {p.code}",
                    callback_data=f"adm_promo_deactivate:{p.id}"
                )])

        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])

        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_promo_list: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "adm_promo_create")
async def adm_promo_create_start(callback: CallbackQuery, state: FSMContext):
    """Начало создания промокода"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    await callback.answer()
    await safe_edit_message(
        callback.message,
        "🎟 **Создание промокода**\n\nВведите код промокода (только латинские буквы и цифры, например `SUMMER25`):",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_promo")]
        ])
    )
    await state.set_state(AdminPromoStates.waiting_code)


@router.message(AdminPromoStates.waiting_code)
async def adm_promo_receive_code(message: Message, state: FSMContext):
    """Получить код промокода"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return

    code = (message.text or "").strip().upper()
    if not code or not code.isalnum():
        await message.answer("❌ Код должен содержать только латинские буквы и цифры. Попробуйте ещё раз.")
        return

    # Проверяем уникальность
    try:
        async with async_session_maker() as session:
            existing = (await session.execute(
                select(PromoCode).where(PromoCode.code == code)
            )).scalar_one_or_none()
        if existing:
            await message.answer("❌ Такой промокод уже существует. Введите другой.")
            return
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    await state.update_data(promo_code=code)
    await message.answer(
        f"✅ Код: `{code}`\n\nВведите размер скидки в % (например `15` для 15%):",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminPromoStates.waiting_discount)


@router.message(AdminPromoStates.waiting_discount)
async def adm_promo_receive_discount(message: Message, state: FSMContext):
    """Получить скидку промокода"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return

    text = (message.text or "").strip().replace("%", "")
    try:
        discount = float(text)
        if not (1 <= discount <= 100):
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите число от 1 до 100.")
        return

    await state.update_data(promo_discount=discount)
    await message.answer(
        "Введите максимальное число использований (например `50`) или `0` для неограниченного:"
    )
    await state.set_state(AdminPromoStates.waiting_max_uses)


@router.message(AdminPromoStates.waiting_max_uses)
async def adm_promo_receive_max_uses(message: Message, state: FSMContext):
    """Получить лимит использований и сохранить промокод"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return

    text = (message.text or "").strip()
    try:
        max_uses_val = int(text)
        if max_uses_val < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое число ≥ 0.")
        return

    data = await state.get_data()
    code = data["promo_code"]
    discount = data["promo_discount"]
    max_uses = max_uses_val if max_uses_val > 0 else None

    try:
        async with async_session_maker() as session:
            promo = PromoCode(
                code=code,
                discount_percent=discount,
                max_uses=max_uses,
                uses_count=0,
                is_active=True,
                created_by=message.from_user.id,
            )
            session.add(promo)
            await session.commit()

        await state.clear()
        max_label = str(max_uses) if max_uses else "∞"
        await message.answer(
            f"✅ **Промокод создан!**\n\n"
            f"🎟 Код: `{code}`\n"
            f"🏷 Скидка: **{discount:.0f}%**\n"
            f"🔢 Лимит использований: **{max_label}**",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎟 К промокодам", callback_data="adm_promo")],
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="adm_back")],
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error creating promo: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка при создании промокода: {str(e)[:100]}")
        await state.clear()


@router.callback_query(F.data.startswith("adm_promo_deactivate:"))
async def adm_promo_deactivate(callback: CallbackQuery):
    """Деактивировать промокод"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    promo_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            promo = await session.get(PromoCode, promo_id)
            if promo:
                promo.is_active = False
                await session.commit()
                await callback.answer(f"✅ Промокод {promo.code} деактивирован")
            else:
                await callback.answer("❌ Промокод не найден", show_alert=True)
                return
        # Обновляем список
        callback.data = "adm_promo"
        await adm_promo_list(callback)
    except Exception as e:
        logger.error(f"Error deactivating promo: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)

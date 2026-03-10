"""
Обработчики для администратора
"""
import logging
import traceback
from datetime import datetime, date as date_type
from decimal import Decimal

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, func

from config import ADMIN_IDS, ADMIN_PASSWORD, CHANNEL_CATEGORIES, AUTOPOST_ENABLED, CLAUDE_API_KEY, TELEMETR_API_TOKEN, MANAGER_LEVELS
from database import async_session_maker, Channel, Manager, Order, ScheduledPost, Competition, Slot, Client
from keyboards import get_admin_panel_menu, get_channel_settings_keyboard, get_category_keyboard
from utils import AdminChannelStates, AdminPasswordState, AdminCompetitionStates


logger = logging.getLogger(__name__)
router = Router()

# Хранилище авторизованных админов
authenticated_admins = set()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

async def safe_edit_message(message, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    """Безопасное редактирование сообщения с обработкой ошибок"""
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            pass  # Игнорируем — сообщение не изменилось
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
            avg_reach = channel.avg_reach_24h or channel.avg_reach or 0
            
            if avg_reach == 0:
                await callback.answer("❌ Нет данных об охвате!", show_alert=True)
                return
            
            price_124 = int(avg_reach * cpm / 1000)
            price_148 = int(price_124 * 0.8)
            price_248 = int(price_124 * 1.6)
            price_native = int(price_124 * 2.5)
            
            channel.prices = {"1/24": price_124, "1/48": price_148, "2/48": price_248, "native": price_native}
            await session.commit()
        
        await safe_edit_message(
            callback.message,
            f"✅ **Цены рассчитаны по CPM!**\n\n"
            f"📊 Охват: {avg_reach:,}\n"
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


# ==================== ОБНОВЛЕНИЕ СТАТИСТИКИ ====================

@router.callback_query(F.data.startswith("adm_ch_update:"))
async def adm_update_channel_stats(callback: CallbackQuery, bot: Bot):
    """Обновить статистику канала"""
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
            
            try:
                chat = await bot.get_chat(channel.telegram_id)
                member_count = await bot.get_chat_member_count(channel.telegram_id)
                
                channel.subscribers = member_count
                channel.name = chat.title or channel.name
                await session.commit()
                
                await callback.answer(f"✅ {member_count:,} подписчиков", show_alert=True)
                
            except TelegramBadRequest as e:
                await callback.answer(f"❌ Бот не админ канала", show_alert=True)
                return
        
        # Обновляем карточку
        text, is_active, _ = await get_channel_card(channel_id)
        if text:
            await safe_edit_message(callback.message, text, get_channel_settings_keyboard(channel_id, is_active))
                
    except Exception as e:
        logger.error(f"Error in adm_update_channel_stats: {traceback.format_exc()}")
        await callback.answer(f"❌ Ошибка", show_alert=True)


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

@router.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    """Статистика бота"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            total_orders = (await session.execute(select(func.count(Order.id)))).scalar() or 0
            total_revenue = (await session.execute(
                select(func.sum(Order.final_price)).where(Order.status == "payment_confirmed")
            )).scalar() or 0
            total_managers = (await session.execute(select(func.count(Manager.id)))).scalar() or 0
            total_channels = (await session.execute(select(func.count(Channel.id)))).scalar() or 0
        
        text = (
            "📊 **Статистика**\n\n"
            f"📦 Заказов: **{total_orders}**\n"
            f"💰 Выручка: **{float(total_revenue):,.0f}₽**\n"
            f"👥 Менеджеров: **{total_managers}**\n"
            f"📢 Каналов: **{total_channels}**"
        )
        
        await safe_edit_message(
            callback.message, text,
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
        )
    except Exception as e:
        logger.error(f"Error in adm_stats: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


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
    """CPM по тематикам"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    text = "💰 **CPM по тематикам**\n\n"
    sorted_categories = sorted(CHANNEL_CATEGORIES.items(), key=lambda x: x[1]["cpm"], reverse=True)[:15]
    for key, cat in sorted_categories:
        text += f"{cat['name']}: **{cat['cpm']:,}₽**\n"
    text += f"\n_Всего: {len(CHANNEL_CATEGORIES)}_"
    
    await safe_edit_message(
        callback.message, text,
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    )


# ==================== НАСТРОЙКИ ====================

@router.callback_query(F.data == "adm_settings")
async def adm_settings(callback: CallbackQuery):
    """Настройки бота"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    text = (
        "⚙️ **Настройки**\n\n"
        f"📝 Автопостинг: {'🟢' if AUTOPOST_ENABLED else '🔴'}\n"
        f"🤖 Claude API: {'🟢' if CLAUDE_API_KEY else '🔴'}\n"
        f"📊 Telemetr API: {'🟢' if TELEMETR_API_TOKEN else '🔴'}\n"
        f"👤 Админы: {len(ADMIN_IDS)}"
    )
    
    await safe_edit_message(
        callback.message, text,
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
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
            f"💰 Сумма: **{float(order.final_price):,.0f}₽**\n"
            f"📊 Статус: {status_text}\n"
        )

        if order.ad_content:
            text += f"\n📝 Текст:\n{order.ad_content[:300]}{'...' if len(order.ad_content) > 300 else ''}\n"

        buttons = []
        if order.status == "payment_uploaded":
            buttons.append([
                InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"adm_confirm_payment:{order_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject_payment:{order_id}")
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
            await session.commit()

            client = await session.get(Client, order.client_id)
            client_telegram_id = client.telegram_id if client else None

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
        scheduled_time = post.scheduled_time.strftime("%d.%m.%Y %H:%M") if post.scheduled_time else "—"

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
    """Текстовая кнопка — статистика"""
    if message.from_user.id not in authenticated_admins and message.from_user.id not in ADMIN_IDS:
        return
    try:
        async with async_session_maker() as session:
            total_orders = (await session.execute(select(func.count(Order.id)))).scalar() or 0
            total_revenue = (await session.execute(
                select(func.sum(Order.final_price)).where(Order.status == "payment_confirmed")
            )).scalar() or 0
            total_managers = (await session.execute(select(func.count(Manager.id)))).scalar() or 0
            total_channels = (await session.execute(select(func.count(Channel.id)))).scalar() or 0

        text = (
            "📊 **Статистика**\n\n"
            f"📦 Заказов: **{total_orders}**\n"
            f"💰 Выручка: **{float(total_revenue):,.0f}₽**\n"
            f"👥 Менеджеров: **{total_managers}**\n"
            f"📢 Каналов: **{total_channels}**"
        )
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
        ]), parse_mode=ParseMode.MARKDOWN)
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
    text = (
        "⚙️ **Настройки**\n\n"
        f"📝 Автопостинг: {'🟢' if AUTOPOST_ENABLED else '🔴'}\n"
        f"🤖 Claude API: {'🟢' if CLAUDE_API_KEY else '🔴'}\n"
        f"📊 Telemetr API: {'🟢' if TELEMETR_API_TOKEN else '🔴'}\n"
        f"👤 Админы: {len(ADMIN_IDS)}"
    )
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
    ]), parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "🚪 Выйти")
async def btn_adm_logout(message: Message):
    """Текстовая кнопка — выйти из админки"""
    if message.from_user.id in authenticated_admins:
        authenticated_admins.discard(message.from_user.id)
        await message.answer("👋 Вы вышли из админ-панели")

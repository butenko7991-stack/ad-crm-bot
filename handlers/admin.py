"""
Обработчики для администратора
"""
import logging
import traceback
from datetime import datetime

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select, func

from config import ADMIN_IDS, ADMIN_PASSWORD, CHANNEL_CATEGORIES, AUTOPOST_ENABLED, CLAUDE_API_KEY, TELEMETR_API_TOKEN, MANAGER_LEVELS
from database import async_session_maker, Channel, Manager, Order, ScheduledPost, Competition
from keyboards import get_admin_panel_menu, get_channel_settings_keyboard, get_category_keyboard
from utils import AdminChannelStates, AdminPasswordState


logger = logging.getLogger(__name__)
router = Router()

# Хранилище авторизованных админов
authenticated_admins = set()


# ==================== АВТОРИЗАЦИЯ ====================

@router.callback_query(F.data == "request_admin_password")
async def request_admin_password(callback: CallbackQuery, state: FSMContext):
    """Запросить пароль админа"""
    await callback.answer()
    await callback.message.answer(
        "🔐 Введите пароль администратора:",
        parse_mode=ParseMode.MARKDOWN
    )
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
    await callback.message.delete()


# ==================== АДМИН-ПАНЕЛЬ ====================

@router.callback_query(F.data == "adm_back")
async def adm_back(callback: CallbackQuery):
    """Назад в админ-панель"""
    await callback.answer()
    await callback.message.edit_text(
        "⚙️ **Админ-панель**\n\nВыберите действие:",
        reply_markup=get_admin_panel_menu(),
        parse_mode=ParseMode.MARKDOWN
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
                buttons.append([InlineKeyboardButton(
                    text=f"⚙️ {ch['name']}",
                    callback_data=f"adm_ch:{ch['id']}"
                )])
            buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "📢 Каналов пока нет"
            buttons = [
                [InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
            ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_channels: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data.startswith("adm_ch:"))
async def adm_channel_settings(callback: CallbackQuery):
    """Настройки канала"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        channel_id = int(callback.data.split(":")[1])
        
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            
            if not channel:
                await callback.message.edit_text("❌ Канал не найден")
                return
            
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
        
        await callback.message.edit_text(
            text,
            reply_markup=get_channel_settings_keyboard(channel_id, ch_data["is_active"]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_channel_settings: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
        
        await callback.message.edit_text(
            f"💰 **Изменение цен для {channel_name}**\n\n"
            f"Текущие цены:\n"
            f"• 1/24: {prices.get('1/24', 0):,}₽\n"
            f"• 1/48: {prices.get('1/48', 0):,}₽\n"
            f"• 2/48: {prices.get('2/48', 0):,}₽\n"
            f"• Навсегда: {prices.get('native', 0):,}₽\n\n"
            f"Выберите что изменить:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
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
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_channel_prices: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
    
    await state.update_data(
        editing_channel_id=channel_id,
        editing_price_type=price_type
    )
    
    price_names = {
        "1/24": "1/24 (24 часа)",
        "1/48": "1/48 (48 часов)",
        "2/48": "2/48 (2 поста)",
        "native": "Навсегда"
    }
    
    await callback.message.edit_text(
        f"💰 **Введите новую цену для {price_names.get(price_type, price_type)}**\n\n"
        f"Отправьте число (только цифры):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_ch_prices:{channel_id}")]
        ]),
        parse_mode=ParseMode.MARKDOWN
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
                [InlineKeyboardButton(text="💰 Продолжить редактирование", callback_data=f"adm_ch_prices:{channel_id}")],
                [InlineKeyboardButton(text="◀️ К настройкам канала", callback_data=f"adm_ch:{channel_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in receive_new_price: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)
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
            
            channel.prices = {
                "1/24": price_124,
                "1/48": price_148,
                "2/48": price_248,
                "native": price_native
            }
            await session.commit()
        
        await callback.message.edit_text(
            f"✅ **Цены рассчитаны по CPM!**\n\n"
            f"📊 Охват: {avg_reach:,}\n"
            f"💰 CPM: {cpm:,.0f}₽\n\n"
            f"**Новые цены:**\n"
            f"• 1/24: {price_124:,}₽\n"
            f"• 1/48: {price_148:,}₽\n"
            f"• 2/48: {price_248:,}₽\n"
            f"• Навсегда: {price_native:,}₽",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К настройкам канала", callback_data=f"adm_ch:{channel_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in auto_calculate_prices: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== ОБНОВЛЕНИЕ СТАТИСТИКИ ====================

@router.callback_query(F.data.startswith("adm_ch_update:"))
async def adm_update_channel_stats(callback: CallbackQuery, bot: Bot):
    """Обновить статистику канала"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer("📊 Обновляю...")
    
    channel_id = int(callback.data.split(":")[1])
    
    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            
            if not channel:
                await callback.message.answer("❌ Канал не найден")
                return
            
            try:
                chat = await bot.get_chat(channel.telegram_id)
                member_count = await bot.get_chat_member_count(channel.telegram_id)
                
                channel.subscribers = member_count
                channel.name = chat.title or channel.name
                await session.commit()
                
                # Показываем обновлённые данные
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
                    f"👥 Подписчиков: **{ch_data['subscribers']:,}** ✅\n"
                    f"👁 Охват 24ч: **{ch_data['avg_reach']:,}**\n"
                    f"💰 CPM: **{ch_data['cpm']:,.0f}₽**\n\n"
                    f"**Цены:**\n"
                    f"• 1/24: {ch_data['prices'].get('1/24', 0):,}₽\n"
                    f"• 1/48: {ch_data['prices'].get('1/48', 0):,}₽\n"
                    f"• 2/48: {ch_data['prices'].get('2/48', 0):,}₽\n"
                    f"• Навсегда: {ch_data['prices'].get('native', 0):,}₽"
                )
                
                await callback.message.edit_text(
                    text,
                    reply_markup=get_channel_settings_keyboard(channel_id, ch_data["is_active"]),
                    parse_mode=ParseMode.MARKDOWN
                )
                
            except Exception as e:
                await callback.message.answer(f"❌ Ошибка обновления: {str(e)[:200]}")
                
    except Exception as e:
        logger.error(f"Error in adm_update_channel_stats: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
                status = "активирован ✅" if channel.is_active else "деактивирован ❌"
                await callback.answer(f"Канал {status}", show_alert=True)
                
                # Показываем обновлённые данные
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
                status_text = "✅ Активен" if ch_data["is_active"] else "❌ Неактивен"
                
                text = (
                    f"⚙️ **Настройки канала**\n\n"
                    f"📢 **{ch_data['name']}**\n"
                    f"👤 @{ch_data['username']}\n"
                    f"{category_info['name']}\n"
                    f"{status_text}\n\n"
                    f"👥 Подписчиков: **{ch_data['subscribers']:,}**\n"
                    f"👁 Охват 24ч: **{ch_data['avg_reach']:,}**\n"
                    f"💰 CPM: **{ch_data['cpm']:,.0f}₽**\n\n"
                    f"**Цены:**\n"
                    f"• 1/24: {ch_data['prices'].get('1/24', 0):,}₽\n"
                    f"• 1/48: {ch_data['prices'].get('1/48', 0):,}₽\n"
                    f"• 2/48: {ch_data['prices'].get('2/48', 0):,}₽\n"
                    f"• Навсегда: {ch_data['prices'].get('native', 0):,}₽"
                )
                
                await callback.message.edit_text(
                    text,
                    reply_markup=get_channel_settings_keyboard(channel_id, ch_data["is_active"]),
                    parse_mode=ParseMode.MARKDOWN
                )
    except Exception as e:
        logger.error(f"Error in adm_toggle_channel: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== УДАЛЕНИЕ КАНАЛА ====================

@router.callback_query(F.data.startswith("adm_ch_delete:"))
async def adm_delete_channel(callback: CallbackQuery):
    """Удалить канал"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    channel_id = int(callback.data.split(":")[1])
    
    await callback.message.edit_text(
        "⚠️ **Удалить канал?**\n\nЭто действие нельзя отменить!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm_ch_del_confirm:{channel_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_ch:{channel_id}")
            ]
        ]),
        parse_mode=ParseMode.MARKDOWN
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
                text += f"{status} **{ch['name']}** (ID: {ch['id']})\n"
                buttons.append([InlineKeyboardButton(
                    text=f"⚙️ {ch['name']}",
                    callback_data=f"adm_ch:{ch['id']}"
                )])
            buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "📢 Каналов пока нет"
            buttons = [
                [InlineKeyboardButton(text="➕ Добавить канал", callback_data="adm_add_channel")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
            ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_delete_channel_confirm: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== ДОБАВЛЕНИЕ КАНАЛА ====================

@router.callback_query(F.data == "adm_add_channel")
async def adm_add_channel(callback: CallbackQuery, state: FSMContext):
    """Добавить канал"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    await callback.message.edit_text(
        "➕ **Добавление канала**\n\n"
        "Перешлите любое сообщение из канала или отправьте @username канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_channels")]
        ]),
        parse_mode=ParseMode.MARKDOWN
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
            await message.answer("❌ Канал не найден. Проверьте username.")
            return
    else:
        await message.answer("❌ Перешлите сообщение из канала или отправьте @username")
        return
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(Channel).where(Channel.telegram_id == channel_id)
        )
        existing = result.scalar_one_or_none()
        
        if existing:
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
        f"📢 **{channel_name}**\n"
        f"👤 @{channel_username or '—'}\n"
        f"👥 Подписчиков: {member_count:,}\n\n"
        f"Выберите тематику канала:",
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
        
        await callback.message.edit_text(
            f"✅ **Канал добавлен!**\n\n"
            f"📢 {data['new_channel_name']}\n"
            f"📁 {category_info['name']}\n\n"
            f"Теперь установите цены:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 Установить цены", callback_data=f"adm_ch_prices:{channel_id}")],
                [InlineKeyboardButton(text="◀️ К списку каналов", callback_data="adm_channels")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error adding channel: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)
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
            result = await session.execute(
                select(Manager).order_by(Manager.total_sales.desc())
            )
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
                text += f"{status} {m['emoji']} **{m['name']}** — {m['total_sales']} продаж, {m['total_earned']:,.0f}₽\n"
                buttons.append([InlineKeyboardButton(
                    text=f"⚙️ {m['name']}",
                    callback_data=f"adm_mgr:{m['id']}"
                )])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "👥 Менеджеров пока нет"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_managers: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
                select(Order)
                .where(Order.status == "payment_uploaded")
                .order_by(Order.created_at.desc())
            )
            orders = result.scalars().all()
            
            orders_data = [{"id": o.id, "price": float(o.final_price or 0)} for o in orders[:10]]
        
        if orders_data:
            text = f"💳 **Оплаты на проверке: {len(orders_data)}**\n\n"
            buttons = []
            for o in orders_data:
                text += f"• Заказ #{o['id']} — {o['price']:,.0f}₽\n"
                buttons.append([InlineKeyboardButton(
                    text=f"📄 Заказ #{o['id']}",
                    callback_data=f"adm_order:{o['id']}"
                )])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "✅ Нет оплат на проверке"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_payments: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
                select(ScheduledPost)
                .where(ScheduledPost.status == "moderation")
                .order_by(ScheduledPost.created_at.desc())
            )
            posts = result.scalars().all()
            
            posts_data = []
            for post in posts[:10]:
                channel = await session.get(Channel, post.channel_id)
                posts_data.append({
                    "id": post.id,
                    "channel_name": channel.name if channel else "N/A"
                })
        
        if posts_data:
            text = f"📝 **Посты на модерации: {len(posts_data)}**\n\n"
            buttons = []
            for post in posts_data:
                text += f"• ID {post['id']} — {post['channel_name']}\n"
                buttons.append([InlineKeyboardButton(
                    text=f"📄 Пост #{post['id']}",
                    callback_data=f"adm_post:{post['id']}"
                )])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "✅ Нет постов на модерации"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_moderation: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
            orders_count = await session.execute(select(func.count(Order.id)))
            total_orders = orders_count.scalar() or 0
            
            revenue_sum = await session.execute(
                select(func.sum(Order.final_price))
                .where(Order.status == "payment_confirmed")
            )
            total_revenue = revenue_sum.scalar() or 0
            
            managers_count = await session.execute(select(func.count(Manager.id)))
            total_managers = managers_count.scalar() or 0
            
            channels_count = await session.execute(select(func.count(Channel.id)))
            total_channels = channels_count.scalar() or 0
        
        text = (
            "📊 **Статистика бота**\n\n"
            f"📦 Всего заказов: **{total_orders}**\n"
            f"💰 Выручка: **{float(total_revenue):,.0f}₽**\n"
            f"👥 Менеджеров: **{total_managers}**\n"
            f"📢 Каналов: **{total_channels}**"
        )
        
        buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_stats: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
                select(Competition)
                .where(Competition.status == "active")
                .order_by(Competition.start_date.desc())
            )
            competitions = result.scalars().all()
        
        if competitions:
            text = "🏆 **Активные соревнования:**\n\n"
            for c in competitions:
                text += f"• {c.name}\n  📅 {c.start_date} — {c.end_date}\n\n"
        else:
            text = "🏆 Нет активных соревнований"
        
        buttons = [
            [InlineKeyboardButton(text="➕ Создать соревнование", callback_data="adm_create_competition")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_competitions: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== CPM ====================

@router.callback_query(F.data == "adm_cpm")
async def adm_cpm(callback: CallbackQuery):
    """CPM по тематикам"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        text = "💰 **CPM по тематикам**\n\n"
        
        sorted_categories = sorted(CHANNEL_CATEGORIES.items(), key=lambda x: x[1]["cpm"], reverse=True)[:15]
        
        for key, cat in sorted_categories:
            text += f"{cat['name']}: **{cat['cpm']:,}₽**\n"
        
        text += f"\n_Всего тематик: {len(CHANNEL_CATEGORIES)}_"
        
        buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_cpm: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== НАСТРОЙКИ ====================

@router.callback_query(F.data == "adm_settings")
async def adm_settings(callback: CallbackQuery):
    """Настройки бота"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    autopost_status = "🟢 Включен" if AUTOPOST_ENABLED else "🔴 Выключен"
    claude_status = "🟢 Настроен" if CLAUDE_API_KEY else "🔴 Не настроен"
    telemetr_status = "🟢 Настроен" if TELEMETR_API_TOKEN else "🔴 Не настроен"
    
    text = (
        "⚙️ **Настройки бота**\n\n"
        f"📝 Автопостинг: {autopost_status}\n"
        f"🤖 Claude API: {claude_status}\n"
        f"📊 Telemetr API: {telemetr_status}\n\n"
        f"👤 Админы: {len(ADMIN_IDS)}"
    )
    
    buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )

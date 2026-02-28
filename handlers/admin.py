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

from config import ADMIN_IDS, ADMIN_PASSWORD, CHANNEL_CATEGORIES, AUTOPOST_ENABLED, CLAUDE_API_KEY, TELEMETR_API_TOKEN
from database import async_session_maker, Channel, Manager, Order, ScheduledPost, Competition
from keyboards import get_admin_panel_menu, get_channel_settings_keyboard, get_category_keyboard
from utils import AdminChannelStates, AdminPasswordState, get_channel_stats_via_bot


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
    # Удаляем сообщение с паролем
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


# ==================== МЕНЕДЖЕРЫ ====================

@router.callback_query(F.data == "adm_managers")
async def adm_managers(callback: CallbackQuery):
    """Список менеджеров"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return
    
    await callback.answer()
    
    try:
        from config import MANAGER_LEVELS
        
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

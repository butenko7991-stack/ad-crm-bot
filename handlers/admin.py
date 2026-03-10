"""
Обработчики для администратора
"""
import logging
import traceback
from datetime import datetime, date as date_type, timezone
from decimal import Decimal

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, func, delete

from config import ADMIN_IDS, ADMIN_PASSWORD, CHANNEL_CATEGORIES, AUTOPOST_ENABLED, CLAUDE_API_KEY, TELEMETR_API_TOKEN, MANAGER_LEVELS
from database import async_session_maker, Channel, CategoryCPM, Manager, Order, ManagerPayout, ScheduledPost, Competition, Slot, Client
from keyboards import get_admin_panel_menu, get_channel_settings_keyboard, get_category_keyboard
from utils import AdminChannelStates, AdminPasswordState, AdminCompetitionStates, AdminCPMStates, AdminSlotStates


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
        from keyboards import get_main_menu
        await message.answer(
            "✅ **Добро пожаловать в админ-панель!**",
            reply_markup=get_main_menu(is_admin=True, is_authenticated_admin=True),
            parse_mode=ParseMode.MARKDOWN
        )
        await message.answer(
            "👇 Выберите раздел:",
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
    from keyboards import get_main_menu
    await callback.answer("👋 Вы вышли из админ-панели", show_alert=True)
    try:
        await callback.message.delete()
    except:
        pass
    await callback.message.answer(
        "👋 Вы вышли из админ-панели.",
        reply_markup=get_main_menu(is_admin=True, is_authenticated_admin=False),
        parse_mode=ParseMode.MARKDOWN
    )


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
    """Авторасчёт цен по CPM (используется CPM из БД, если задан вручную)"""
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
            
            avg_reach = channel.avg_reach_24h or channel.avg_reach or 0
            if avg_reach == 0:
                await callback.answer("❌ Нет данных об охвате!", show_alert=True)
                return

            # Приоритет CPM: ручной для канала → ручной для категории (БД) → конфиг
            if channel.cpm and float(channel.cpm) > 0:
                cpm = float(channel.cpm)
                cpm_source = "канала"
            else:
                cat_key = channel.category or "other"
                db_cpm_row = (await session.execute(
                    select(CategoryCPM).where(CategoryCPM.category_key == cat_key)
                )).scalar_one_or_none()
                if db_cpm_row and db_cpm_row.cpm:
                    cpm = float(db_cpm_row.cpm)
                    cpm_source = "категории (ручной)"
                else:
                    cpm = float(CHANNEL_CATEGORIES.get(cat_key, {}).get("cpm", 1000))
                    cpm_source = "категории (конфиг)"

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
            f"💰 CPM ({cpm_source}): {cpm:,.0f}₽\n\n"
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

async def _get_effective_cpm(session, category_key: str) -> int:
    """Получить CPM категории: из БД если задан, иначе из конфига"""
    db_cpm = await session.execute(
        select(CategoryCPM).where(CategoryCPM.category_key == category_key)
    )
    db_row = db_cpm.scalar_one_or_none()
    if db_row and db_row.cpm:
        return db_row.cpm
    return CHANNEL_CATEGORIES.get(category_key, {}).get("cpm", 0)


@router.callback_query(F.data == "adm_cpm")
async def adm_cpm(callback: CallbackQuery, state: FSMContext):
    """CPM по тематикам — список с кнопками редактирования"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    await state.clear()
    await _show_cpm_page(callback.message, 0)


@router.callback_query(F.data.startswith("adm_cpm_page:"))
async def adm_cpm_page(callback: CallbackQuery, state: FSMContext):
    """Пагинация списка CPM"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    page = int(callback.data.split(":")[1])
    await _show_cpm_page(callback.message, page)


async def _show_cpm_page(message, page: int):
    """Отобразить страницу списка CPM категорий"""
    PAGE_SIZE = 10
    sorted_cats = sorted(CHANNEL_CATEGORIES.items(), key=lambda x: x[1]["cpm"], reverse=True)
    total = len(sorted_cats)
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, max(0, total_pages - 1)))

    slice_cats = sorted_cats[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    # Получаем все переопределения из БД за один запрос
    async with async_session_maker() as session:
        db_result = await session.execute(select(CategoryCPM))
        db_cpms = {row.category_key: row.cpm for row in db_result.scalars().all()}

    text = f"💰 **CPM по тематикам** (стр. {page + 1}/{total_pages})\n\n"
    buttons = []
    for key, cat in slice_cats:
        effective = db_cpms.get(key) or cat["cpm"]
        marker = " ✏️" if key in db_cpms else ""
        text += f"{cat['name']}: **{effective:,}₽**{marker}\n"
        buttons.append([InlineKeyboardButton(
            text=f"✏️ {cat['name']} ({effective:,}₽)",
            callback_data=f"adm_cpm_edit:{key}"
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_cpm_page:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_cpm_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="🔄 Сбросить все", callback_data="adm_cpm_reset_all")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])

    text += "\n_✏️ — значение изменено вручную_"

    await safe_edit_message(
        message, text,
        InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("adm_cpm_edit:"))
async def adm_cpm_edit_start(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование CPM категории"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    category_key = callback.data.split(":", 1)[1]
    cat_info = CHANNEL_CATEGORIES.get(category_key)
    if not cat_info:
        await callback.answer("❌ Категория не найдена", show_alert=True)
        return

    async with async_session_maker() as session:
        effective = await _get_effective_cpm(session, category_key)

    await state.update_data(editing_cpm_key=category_key)

    await safe_edit_message(
        callback.message,
        f"✏️ **Редактирование CPM**\n\n"
        f"Категория: {cat_info['name']}\n"
        f"Текущий CPM: **{effective:,}₽**\n"
        f"(базовый из конфига: {cat_info['cpm']:,}₽)\n\n"
        f"Введите новое значение CPM (целое число, ₽ за 1000 просмотров):",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Сбросить к базовому", callback_data=f"adm_cpm_reset:{category_key}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cpm")]
        ])
    )
    await state.set_state(AdminCPMStates.waiting_cpm_value)


@router.message(AdminCPMStates.waiting_cpm_value)
async def adm_cpm_receive_value(message: Message, state: FSMContext):
    """Сохранить новое значение CPM для категории"""
    try:
        new_cpm = int(message.text.strip().replace(" ", "").replace(",", ""))
        if new_cpm <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите целое положительное число (например: 2500)")
        return

    data = await state.get_data()
    category_key = data.get("editing_cpm_key")
    if not category_key:
        await message.answer("❌ Ошибка. Начните заново.")
        await state.clear()
        return

    cat_info = CHANNEL_CATEGORIES.get(category_key, {})

    try:
        async with async_session_maker() as session:
            db_result = await session.execute(
                select(CategoryCPM).where(CategoryCPM.category_key == category_key)
            )
            row = db_result.scalar_one_or_none()
            if row:
                row.cpm = new_cpm
                row.updated_at = datetime.now(timezone.utc)
                row.updated_by = message.from_user.id
            else:
                row = CategoryCPM(
                    category_key=category_key,
                    name=cat_info.get("name", category_key),
                    cpm=new_cpm,
                    updated_by=message.from_user.id,
                )
                session.add(row)
            await session.commit()

        await state.clear()

        await message.answer(
            f"✅ **CPM обновлён!**\n\n"
            f"Категория: {cat_info.get('name', category_key)}\n"
            f"Новый CPM: **{new_cpm:,}₽**",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К списку CPM", callback_data="adm_cpm")],
                [InlineKeyboardButton(text="◀️ Назад в панель", callback_data="adm_back")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_cpm_receive_value: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)
        await state.clear()


@router.callback_query(F.data.startswith("adm_cpm_reset:"))
async def adm_cpm_reset_one(callback: CallbackQuery, state: FSMContext):
    """Сбросить CPM одной категории к базовому значению из конфига"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    category_key = callback.data.split(":", 1)[1]

    try:
        async with async_session_maker() as session:
            db_result = await session.execute(
                select(CategoryCPM).where(CategoryCPM.category_key == category_key)
            )
            row = db_result.scalar_one_or_none()
            if row:
                await session.delete(row)
                await session.commit()

        await state.clear()
        cat_name = CHANNEL_CATEGORIES.get(category_key, {}).get("name", category_key)
        base_cpm = CHANNEL_CATEGORIES.get(category_key, {}).get("cpm", 0)
        await callback.answer(f"🔄 Сброшено! Базовый CPM: {base_cpm:,}₽", show_alert=True)
        await _show_cpm_page(callback.message, 0)
    except Exception as e:
        logger.error(f"Error in adm_cpm_reset_one: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "adm_cpm_reset_all")
async def adm_cpm_reset_all(callback: CallbackQuery, state: FSMContext):
    """Сбросить все CPM к базовым значениям"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    await safe_edit_message(
        callback.message,
        "⚠️ **Сбросить все CPM к базовым значениям?**\n\n"
        "Все вручную заданные значения будут удалены.",
        InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, сбросить", callback_data="adm_cpm_reset_all_confirm"),
                InlineKeyboardButton(text="❌ Нет", callback_data="adm_cpm")
            ]
        ])
    )


@router.callback_query(F.data == "adm_cpm_reset_all_confirm")
async def adm_cpm_reset_all_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтвердить сброс всех CPM"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        async with async_session_maker() as session:
            await session.execute(delete(CategoryCPM))
            await session.commit()

        await callback.answer("🔄 Все CPM сброшены к базовым значениям", show_alert=True)
        await _show_cpm_page(callback.message, 0)
    except Exception as e:
        logger.error(f"Error in adm_cpm_reset_all_confirm: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


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


# ==================== УПРАВЛЕНИЕ ВЫПЛАТАМИ МЕНЕДЖЕРАМ ====================

@router.callback_query(F.data == "adm_payouts")
async def adm_payouts(callback: CallbackQuery):
    """Список заявок на выплату"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ManagerPayout)
                .where(ManagerPayout.status == "pending")
                .order_by(ManagerPayout.created_at.asc())
            )
            payouts = result.scalars().all()

            payout_rows = []
            for p in payouts[:15]:
                mgr = await session.get(Manager, p.manager_id)
                payout_rows.append({
                    "id": p.id,
                    "amount": float(p.amount),
                    "method": p.method,
                    "name": (mgr.first_name or mgr.username or f"ID {mgr.telegram_id}") if mgr else "—",
                })

        if payout_rows:
            text = f"💸 **Заявки на выплату: {len(payout_rows)}**\n\n"
            buttons = []
            for p in payout_rows:
                text += f"• #{p['id']} — {p['name']} — {p['amount']:,.0f}₽ ({p['method']})\n"
                buttons.append([InlineKeyboardButton(
                    text=f"💳 #{p['id']} {p['name']} {p['amount']:,.0f}₽",
                    callback_data=f"adm_payout:{p['id']}"
                )])
            buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")])
        else:
            text = "✅ Нет ожидающих выплат"
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]]

        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_payouts: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_payout:"))
async def adm_view_payout(callback: CallbackQuery):
    """Просмотр заявки на выплату"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()

    try:
        payout_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            payout = await session.get(ManagerPayout, payout_id)
            if not payout:
                await callback.message.answer("❌ Заявка не найдена")
                return
            mgr = await session.get(Manager, payout.manager_id)

        name = (mgr.first_name or mgr.username or f"ID {mgr.telegram_id}") if mgr else "—"
        method_names = {"card": "💳 Карта", "sbp": "📱 СБП"}
        created = payout.created_at.strftime("%d.%m.%Y %H:%M") if payout.created_at else "—"

        text = (
            f"💸 **Заявка #{payout_id}**\n\n"
            f"👤 Менеджер: {name}\n"
            f"💰 Сумма: **{float(payout.amount):,.0f}₽**\n"
            f"📱 Способ: {method_names.get(payout.method, payout.method or '—')}\n"
            f"📋 Реквизиты: `{payout.details or '—'}`\n"
            f"📅 Дата заявки: {created}\n"
            f"📊 Статус: ⏳ Ожидает"
        )

        buttons = []
        if payout.status == "pending":
            buttons.append([
                InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm_payout_approve:{payout_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_payout_reject:{payout_id}")
            ])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="adm_payouts")])

        await safe_edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in adm_view_payout: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_payout_approve:"))
async def adm_approve_payout(callback: CallbackQuery, bot: Bot):
    """Одобрить выплату менеджеру"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        payout_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            payout = await session.get(ManagerPayout, payout_id)
            if not payout:
                await callback.answer("❌ Заявка не найдена", show_alert=True)
                return

            payout.status = "completed"
            payout.processed_at = datetime.now(timezone.utc)
            payout.processed_by = callback.from_user.id
            await session.commit()

            mgr = await session.get(Manager, payout.manager_id)
            mgr_telegram_id = mgr.telegram_id if mgr else None

        await callback.answer("✅ Выплата одобрена!", show_alert=True)

        if mgr_telegram_id:
            try:
                await bot.send_message(
                    mgr_telegram_id,
                    f"✅ **Ваша заявка на выплату #{payout_id} одобрена!**\n\n"
                    f"💰 Сумма: {float(payout.amount):,.0f}₽\n"
                    f"Средства будут переведены на указанные реквизиты.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

        await safe_edit_message(
            callback.message,
            f"✅ **Выплата #{payout_id} одобрена**",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К выплатам", callback_data="adm_payouts")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_approve_payout: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_payout_reject:"))
async def adm_reject_payout(callback: CallbackQuery, bot: Bot):
    """Отклонить выплату — вернуть средства на баланс менеджера"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    try:
        payout_id = int(callback.data.split(":")[1])

        async with async_session_maker() as session:
            payout = await session.get(ManagerPayout, payout_id)
            if not payout:
                await callback.answer("❌ Заявка не найдена", show_alert=True)
                return

            payout.status = "rejected"
            payout.processed_at = datetime.now(timezone.utc)
            payout.processed_by = callback.from_user.id

            # Возвращаем средства на баланс менеджера
            mgr = await session.get(Manager, payout.manager_id)
            if mgr is not None:
                mgr.balance += payout.amount
            await session.commit()

            mgr_telegram_id = mgr.telegram_id if mgr is not None else None

        await callback.answer("❌ Выплата отклонена", show_alert=True)

        if mgr_telegram_id:
            try:
                await bot.send_message(
                    mgr_telegram_id,
                    f"❌ **Заявка на выплату #{payout_id} отклонена.**\n\n"
                    f"💰 Сумма {float(payout.amount):,.0f}₽ возвращена на ваш баланс.\n"
                    f"Свяжитесь с администратором для уточнения.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

        await safe_edit_message(
            callback.message,
            f"❌ **Выплата #{payout_id} отклонена**\n\nСредства возвращены на баланс менеджера.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К выплатам", callback_data="adm_payouts")]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_reject_payout: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


# ==================== УПРАВЛЕНИЕ СЛОТАМИ КАНАЛА ====================

@router.callback_query(F.data.startswith("adm_ch_slots:"))
async def adm_ch_slots(callback: CallbackQuery, state: FSMContext):
    """Управление слотами канала"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    channel_id = int(callback.data.split(":")[1])
    await _show_slots_page(callback.message, channel_id)


async def _show_slots_page(message, channel_id: int):
    """Отобразить список слотов канала"""
    from datetime import date as date_cls
    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                await message.answer("❌ Канал не найден")
                return

            result = await session.execute(
                select(Slot)
                .where(Slot.channel_id == channel_id, Slot.slot_date >= date_cls.today())
                .order_by(Slot.slot_date, Slot.slot_time)
            )
            slots = result.scalars().all()
            slots_data = [
                {
                    "id": s.id,
                    "date": s.slot_date.strftime("%d.%m.%Y"),
                    "time": s.slot_time.strftime("%H:%M"),
                    "status": s.status,
                }
                for s in slots[:20]
            ]

        status_emoji = {"available": "🟢", "reserved": "🟡", "booked": "🔴"}

        if slots_data:
            text = f"📅 **Слоты канала {channel.name}**\n\n"
            buttons = []
            for s in slots_data:
                emoji = status_emoji.get(s["status"], "⚪")
                text += f"{emoji} {s['date']} {s['time']} — {s['status']}\n"
                if s["status"] == "available":
                    buttons.append([InlineKeyboardButton(
                        text=f"🗑 {s['date']} {s['time']}",
                        callback_data=f"adm_slot_del:{s['id']}"
                    )])
        else:
            text = f"📅 **Слоты канала {channel.name}**\n\n_Нет предстоящих слотов_"
            buttons = []

        buttons.append([InlineKeyboardButton(
            text="➕ Добавить слот",
            callback_data=f"adm_slot_add:{channel_id}"
        )])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm_ch:{channel_id}")])

        await safe_edit_message(message, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception as e:
        logger.error(f"Error in _show_slots_page: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("adm_slot_add:"))
async def adm_slot_add_start(callback: CallbackQuery, state: FSMContext):
    """Начать добавление нового слота"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    channel_id = int(callback.data.split(":")[1])
    await state.update_data(slot_channel_id=channel_id)

    await safe_edit_message(
        callback.message,
        "📅 **Добавление слота**\n\nВведите дату в формате ДД.ММ.ГГГГ:",
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_ch_slots:{channel_id}")]
        ])
    )
    await state.set_state(AdminSlotStates.waiting_date)


@router.message(AdminSlotStates.waiting_date)
async def adm_slot_receive_date(message: Message, state: FSMContext):
    """Получить дату нового слота"""
    from datetime import date as date_cls
    try:
        slot_date = datetime.strptime(message.text.strip(), "%d.%m.%Y").date()
        if slot_date < date_cls.today():
            await message.answer("❌ Дата не может быть в прошлом. Введите дату снова:")
            return
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату в формате ДД.ММ.ГГГГ:")
        return

    await state.update_data(slot_date=slot_date.isoformat())
    data = await state.get_data()
    channel_id = data.get("slot_channel_id")

    await message.answer(
        f"✅ Дата: **{slot_date.strftime('%d.%m.%Y')}**\n\nВведите время в формате ЧЧ:ММ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_ch_slots:{channel_id}")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminSlotStates.waiting_time)


@router.message(AdminSlotStates.waiting_time)
async def adm_slot_receive_time(message: Message, state: FSMContext):
    """Получить время и сохранить слот"""
    from datetime import time as time_cls, date as date_cls
    try:
        parsed = datetime.strptime(message.text.strip(), "%H:%M")
        slot_time = parsed.time()
    except ValueError:
        await message.answer("❌ Неверный формат. Введите время в формате ЧЧ:ММ:")
        return

    data = await state.get_data()
    channel_id = data.get("slot_channel_id")
    slot_date = date_cls.fromisoformat(data["slot_date"])

    try:
        async with async_session_maker() as session:
            # Проверяем, нет ли уже такого слота
            existing = await session.execute(
                select(Slot).where(
                    Slot.channel_id == channel_id,
                    Slot.slot_date == slot_date,
                    Slot.slot_time == slot_time
                )
            )
            if existing.scalar_one_or_none():
                await message.answer(
                    f"⚠️ Слот на {slot_date.strftime('%d.%m.%Y')} {slot_time.strftime('%H:%M')} уже существует.\n"
                    f"Введите другое время:"
                )
                return

            new_slot = Slot(
                channel_id=channel_id,
                slot_date=slot_date,
                slot_time=slot_time,
                status="available"
            )
            session.add(new_slot)
            await session.commit()

        await state.clear()
        await message.answer(
            f"✅ **Слот добавлен!**\n\n"
            f"📅 {slot_date.strftime('%d.%m.%Y')} {slot_time.strftime('%H:%M')}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📅 К слотам", callback_data=f"adm_ch_slots:{channel_id}")],
                [InlineKeyboardButton(text="◀️ К каналу", callback_data=f"adm_ch:{channel_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in adm_slot_receive_time: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)[:200]}`", parse_mode=ParseMode.MARKDOWN)
        await state.clear()


@router.callback_query(F.data.startswith("adm_slot_del:"))
async def adm_slot_del_confirm(callback: CallbackQuery):
    """Подтверждение удаления слота"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    await callback.answer()
    slot_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            slot = await session.get(Slot, slot_id)
            if not slot:
                await callback.answer("❌ Слот не найден", show_alert=True)
                return
            channel_id = slot.channel_id
            slot_info = f"{slot.slot_date.strftime('%d.%m.%Y')} {slot.slot_time.strftime('%H:%M')}"

        await safe_edit_message(
            callback.message,
            f"⚠️ **Удалить слот?**\n\n📅 {slot_info}",
            InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да", callback_data=f"adm_slot_del_ok:{slot_id}"),
                    InlineKeyboardButton(text="❌ Нет", callback_data=f"adm_ch_slots:{channel_id}")
                ]
            ])
        )
    except Exception as e:
        logger.error(f"Error in adm_slot_del_confirm: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("adm_slot_del_ok:"))
async def adm_slot_del_execute(callback: CallbackQuery):
    """Выполнить удаление слота"""
    if callback.from_user.id not in authenticated_admins and callback.from_user.id not in ADMIN_IDS:
        await callback.answer("🔐 Требуется авторизация", show_alert=True)
        return

    slot_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            slot = await session.get(Slot, slot_id)
            if not slot:
                await callback.answer("❌ Слот не найден", show_alert=True)
                return
            channel_id = slot.channel_id
            await session.delete(slot)
            await session.commit()

        await callback.answer("🗑 Слот удалён", show_alert=True)
        await _show_slots_page(callback.message, channel_id)
    except Exception as e:
        logger.error(f"Error in adm_slot_del_execute: {traceback.format_exc()}")
        await callback.answer("❌ Ошибка", show_alert=True)

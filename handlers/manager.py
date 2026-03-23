"""
Обработчики для менеджеров
"""
import logging
import traceback
from datetime import datetime, date, timedelta
from decimal import Decimal

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from config import MANAGER_LEVELS, CHANNEL_CATEGORIES, ADMIN_IDS, LOCAL_TZ_OFFSET, LOCAL_TZ_LABEL, OWNER_ID
from database import async_session_maker, Manager, Order, Client, Channel, ManagerPayout, Slot, ScheduledPost
from keyboards import get_manager_cabinet_menu, get_payout_keyboard, get_training_menu, get_calendar_keyboard, get_timezone_keyboard
from utils import ManagerStates, ManagerPostStates, ManagerRegisterStates, ManagerSettingsStates, channel_link
from utils.helpers import escape_md
from services import gamification_service
from services.settings import get_setting, PAYMENT_LINK_KEY


logger = logging.getLogger(__name__)
router = Router()

# XP thresholds per level: (level_start_xp, next_level_start_xp)
_XP_LEVEL_RANGES = {
    1: (0, 200),
    2: (200, 800),
    3: (800, 2000),
    4: (2000, 5000),
    5: (5000, None),
}


def _xp_progress_line(level: int, xp: int) -> str:
    """Format XP progress line for the manager cabinet message."""
    xp_range = _XP_LEVEL_RANGES.get(level)
    if xp_range is None or xp_range[1] is None:
        return f"⭐ Опыт: **{xp} XP** — максимальный уровень"
    level_start, level_end = xp_range
    bar_width = 10
    progress = max(0, xp - level_start)
    total = level_end - level_start
    filled = min(bar_width, int(progress / total * bar_width))
    bar = "█" * filled + "░" * (bar_width - filled)
    next_level_info = MANAGER_LEVELS.get(level + 1, {})
    next_name = next_level_info.get("name", "")
    remaining = level_end - xp
    return f"⭐ Опыт: **{xp} XP** [{bar}] → ещё {remaining} XP до **{next_name}**"


def _build_manager_cabinet_text(manager) -> str:
    """Build the main manager cabinet message text."""
    level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
    name = manager.first_name or manager.username or "Менеджер"
    balance = float(manager.balance or 0)
    total_sales = manager.total_sales or 0
    total_revenue = float(manager.total_revenue or 0)
    total_earned = float(manager.total_earned or 0)
    xp = manager.experience_points or 0
    commission = float(manager.commission_rate or level_info.get("commission", 10))
    return (
        f"👤 **Кабинет менеджера**\n\n"
        f"{level_info['emoji']} **{name}**\n"
        f"📊 Уровень: **{manager.level} — {level_info['name']}** | 🎓 Комиссия: **{commission:.0f}%**\n"
        f"{_xp_progress_line(manager.level, xp)}\n\n"
        f"💰 Баланс: **{balance:,.0f}₽**\n"
        f"📦 Продаж: **{total_sales}** | 💵 Выручка: **{total_revenue:,.0f}₽**\n"
        f"💸 Всего заработано: **{total_earned:,.0f}₽**"
    )


# Часы удаления поста по формату размещения
FORMAT_DELETE_HOURS = {
    "1/24": 24,
    "1/48": 48,
    "2/48": 48,
    "native": 0,
}


def _manager_tz(manager) -> tuple[timedelta, str]:
    """Вернуть (timedelta, label) для timezone менеджера.

    Если менеджер не найден или timezone_offset не задан — используется UTC+3 (Москва).
    """
    offset_hours: int = (manager.timezone_offset if manager and manager.timezone_offset is not None else 3)
    return timedelta(hours=offset_hours), f"UTC{offset_hours:+d}"


# ==================== РЕГИСТРАЦИЯ МЕНЕДЖЕРА ====================

@router.callback_query(F.data == "manager_register")
async def manager_register(callback: CallbackQuery, state: FSMContext):
    """Шаг 1 регистрации — выбор часового пояса"""
    await callback.answer()

    user = callback.from_user

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == user.id)
            )
            existing = result.scalar_one_or_none()

            if existing:
                await callback.message.edit_text(
                    "✅ Вы уже зарегистрированы как менеджер!\n\n"
                    "Используйте /manager для входа в кабинет.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

        await state.set_state(ManagerRegisterStates.selecting_timezone)
        await callback.message.edit_text(
            "🌍 **Выберите ваш часовой пояс**\n\n"
            "Это нужно для правильного отображения времени публикаций.\n"
            "Вы сможете изменить его позже в **⚙️ Настройках** кабинета.",
            reply_markup=get_timezone_keyboard(current_offset=3, context="register"),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in manager_register: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("mgr_tz_register:"), ManagerRegisterStates.selecting_timezone)
async def manager_register_tz_selected(callback: CallbackQuery, state: FSMContext):
    """Шаг 2 регистрации — timezone выбран, создаём менеджера"""
    await callback.answer()

    tz_offset = int(callback.data.split(":")[1])
    user = callback.from_user

    try:
        async with async_session_maker() as session:
            # Повторная проверка на случай гонки
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == user.id)
            )
            if result.scalar_one_or_none():
                await state.clear()
                await callback.message.edit_text(
                    "✅ Вы уже зарегистрированы как менеджер!\n\n"
                    "Используйте /manager для входа в кабинет.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

            manager = Manager(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name or user.username or "Менеджер",
                status="trainee",
                level=1,
                commission_rate=Decimal("10"),
                timezone_offset=tz_offset,
            )
            session.add(manager)
            await session.commit()

        await state.clear()
        tz_label = f"UTC{tz_offset:+d}"
        await callback.message.edit_text(
            f"🎉 **Добро пожаловать в команду!**\n\n"
            f"Вы успешно зарегистрированы как менеджер.\n"
            f"🌍 Ваш часовой пояс: **{tz_label}**\n\n"
            f"**Что дальше:**\n"
            f"📚 Пройдите обучение — /training\n"
            f"💼 Начните продавать — /sales\n"
            f"💰 Получайте комиссию 10-25%\n\n"
            f"Нажмите /manager для входа в кабинет.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in manager_register_tz_selected: {traceback.format_exc()}")
        await state.clear()
        await callback.message.answer(f"❌ Ошибка регистрации: {str(e)[:100]}")


# ==================== КАБИНЕТ МЕНЕДЖЕРА / НАСТРОЙКИ ====================

@router.callback_query(F.data == "mgr_settings")
async def mgr_settings(callback: CallbackQuery):
    """Настройки менеджера — показать текущие параметры"""
    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()

        if not manager:
            await callback.message.edit_text("❌ Вы не менеджер")
            return

        tz_offset = manager.timezone_offset if manager.timezone_offset is not None else 3
        tz_label = f"UTC{tz_offset:+d}"
        await callback.message.edit_text(
            f"⚙️ **Настройки кабинета**\n\n"
            f"🌍 Часовой пояс: **{tz_label}**\n\n"
            f"Все времена публикаций отображаются в вашем часовом поясе.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌍 Изменить часовой пояс", callback_data="mgr_change_timezone")],
                [InlineKeyboardButton(text="◀️ В кабинет", callback_data="mgr_back")],
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_settings: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data == "mgr_change_timezone")
async def mgr_change_timezone(callback: CallbackQuery, state: FSMContext):
    """Показать выбор нового часового пояса"""
    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()

        if not manager:
            await callback.message.edit_text("❌ Вы не менеджер")
            return

        current_offset = manager.timezone_offset if manager.timezone_offset is not None else 3
        await state.set_state(ManagerSettingsStates.selecting_timezone)
        await callback.message.edit_text(
            "🌍 **Выберите новый часовой пояс:**\n\n"
            "Текущий выбор отмечен ✅",
            reply_markup=get_timezone_keyboard(current_offset=current_offset, context="settings"),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_change_timezone: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("mgr_tz_settings:"), ManagerSettingsStates.selecting_timezone)
async def mgr_settings_tz_selected(callback: CallbackQuery, state: FSMContext):
    """Сохранить новый часовой пояс из настроек"""
    await callback.answer()

    tz_offset = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()

            if not manager:
                await state.clear()
                await callback.message.edit_text("❌ Вы не менеджер")
                return

            manager.timezone_offset = tz_offset
            await session.commit()

        await state.clear()
        tz_label = f"UTC{tz_offset:+d}"
        await callback.message.edit_text(
            f"✅ Часовой пояс обновлён: **{tz_label}**\n\n"
            f"Время публикаций теперь отображается в вашем часовом поясе.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Настройки", callback_data="mgr_settings")],
                [InlineKeyboardButton(text="◀️ В кабинет", callback_data="mgr_back")],
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_settings_tz_selected: {traceback.format_exc()}")
        await state.clear()
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")

@router.callback_query(F.data == "mgr_back")
async def mgr_back(callback: CallbackQuery):
    """Назад в кабинет менеджера"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await callback.message.edit_text("❌ Вы не менеджер")
                return
        
        await callback.message.edit_text(
            _build_manager_cabinet_text(manager),
            reply_markup=get_manager_cabinet_menu(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_back: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


# ==================== АНАЛИЗ КАНАЛА ДЛЯ МЕНЕДЖЕРА ====================

@router.callback_query(F.data.startswith("analyze_ch:"))
async def analyze_channel_for_manager(callback: CallbackQuery):
    """Показать карточку канала для менеджера"""
    await callback.answer()
    
    try:
        channel_id = int(callback.data.split(":")[1])
        
        async with async_session_maker() as session:
            # Проверяем что это менеджер
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return
            
            # Получаем канал
            channel = await session.get(Channel, channel_id)
            
            if not channel:
                await callback.message.answer("❌ Канал не найден")
                return
            
            # Формируем карточку канала
            prices = channel.prices or {}
            category_info = CHANNEL_CATEGORIES.get(channel.category, {})
            category_name = category_info.get("name", channel.category or "—")
            
            text = f"📢 **{channel_link(channel.name, channel.username)}**\n\n"
            text += f"📂 Категория: {category_name}\n"
            text += f"👥 Подписчиков: {channel.subscribers or 0:,}\n"
            
            if channel.err_percent:
                text += f"📊 ER: {channel.err_percent}%\n"
            if channel.avg_reach:
                text += f"👁 Средние охваты: {channel.avg_reach:,}\n"
            
            text += f"\n**💰 Цены:**\n"
            for format_type, price in prices.items():
                text += f"• {format_type}: **{price:,}₽**\n"
            
            # Добавляем подсказки для продажи
            commission = MANAGER_LEVELS.get(manager.level, {}).get("commission", 10)
            min_price = min(prices.values()) if prices else 0
            potential_earning = int(min_price * commission / 100)
            
            text += f"\n💡 **Ваша комиссия:** {commission}%\n"
            text += f"💵 Минимальный заработок: ~{potential_earning:,}₽\n"
            text += f"\n📤 Отправьте клиенту реф-ссылку!"
        
        buttons = [
            [InlineKeyboardButton(text="📝 Подать пост на модерацию", callback_data=f"mgr_submit_post:{channel_id}")],
            [InlineKeyboardButton(text="🔗 Моя реф-ссылка", callback_data="copy_ref_link")],
            [InlineKeyboardButton(text="◀️ К каналам", callback_data="back_to_sales")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in analyze_channel_for_manager: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data == "back_to_sales")
async def back_to_sales(callback: CallbackQuery):
    """Вернуться к списку каналов для продаж"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return
            
            result = await session.execute(
                select(Channel).where(Channel.is_active == True)
            )
            channels = result.scalars().all()
            
            channels_data = [{
                "id": ch.id,
                "name": ch.name,
                "username": ch.username,
                "prices": ch.prices or {}
            } for ch in channels]
        
        if not channels_data:
            await callback.message.edit_text("😔 Каналов пока нет")
            return
        
        text = "💼 **Каналы для продажи:**\n\n"
        buttons = []
        
        for ch in channels_data:
            prices = ch["prices"]
            price_124 = prices.get("1/24", 0)
            text += f"📢 **{channel_link(ch['name'], ch.get('username'))}** — от {price_124:,}₽\n"
            buttons.append([InlineKeyboardButton(
                text=f"📊 {ch['name']}",
                callback_data=f"analyze_ch:{ch['id']}"
            )])
        
        buttons.append([InlineKeyboardButton(text="📋 Моя реф-ссылка", callback_data="copy_ref_link")])
        buttons.append([InlineKeyboardButton(text="💡 Как работает схема?", callback_data="mgr_sales_howto")])
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in back_to_sales: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


# ==================== КАК РАБОТАЕТ СХЕМА ====================

@router.callback_query(F.data == "mgr_sales_howto")
async def mgr_sales_howto(callback: CallbackQuery):
    """Объяснение схемы работы рекламодатель→менеджер"""
    await callback.answer()

    text = (
        "💡 **Как работает схема оплаты**\n\n"
        "Вы — менеджер-посредник. Рекламодатель платит напрямую владельцу бота.\n"
        "Ваша задача — привести клиента и помочь ему оформить заказ.\n\n"

        "**📋 Пошагово:**\n\n"
        "1️⃣ **Вы находите рекламодателя** (через соцсети, чаты, знакомых)\n\n"
        "2️⃣ **Отправляете ему свою реф-ссылку** — нажмите «📋 Моя реф-ссылка»\n\n"
        "3️⃣ **Рекламодатель переходит по ссылке** в бот, выбирает канал, "
        "дату и формат размещения\n\n"
        "4️⃣ **Бот показывает сумму и реквизиты для оплаты** "
        "(карта / СБП / ссылка на оплату)\n\n"
        "5️⃣ **Рекламодатель переводит деньги** на указанные реквизиты "
        "и загружает скриншот перевода\n\n"
        "6️⃣ **Администратор проверяет оплату** и подтверждает заказ\n\n"
        "7️⃣ **Ваша комиссия автоматически зачисляется на баланс** 💰\n\n"

        "**💸 Как получить деньги?**\n"
        "Перейдите в «Кабинет менеджера» → «💰 Вывод средств» "
        "и укажите свои реквизиты для выплаты."
    )

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Моя реф-ссылка", callback_data="copy_ref_link")],
            [InlineKeyboardButton(text="◀️ К каналам", callback_data="back_to_sales")],
        ]),
        parse_mode=ParseMode.MARKDOWN
    )


# ==================== МОИ ПРОДАЖИ ====================

@router.callback_query(F.data == "mgr_my_sales")
async def mgr_my_sales(callback: CallbackQuery):
    """Показать продажи менеджера"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return
            
            # Общие данные
            total_sales = manager.total_sales or 0
            total_revenue = float(manager.total_revenue or 0)
            total_earned = float(manager.total_earned or 0)
            commission = float(manager.commission_rate or 10)
            manager_id = manager.id

            # Статистика за текущий месяц
            today = utc_now()
            month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            month_result = await session.execute(
                select(Order)
                .where(Order.manager_id == manager_id)
                .where(Order.status == "payment_confirmed")
                .where(Order.paid_at >= month_start)
                .order_by(Order.paid_at.desc())
            )
            month_orders = month_result.scalars().all()

            month_sales = len(month_orders)
            month_revenue = sum(float(o.final_price or 0) for o in month_orders)
            month_earned = month_revenue * commission / 100

            # Последние 5 подтверждённых заказов
            recent_result = await session.execute(
                select(Order)
                .where(Order.manager_id == manager_id)
                .where(Order.status == "payment_confirmed")
                .order_by(Order.paid_at.desc())
                .limit(5)
            )
            recent_orders = recent_result.scalars().all()

            # Получаем названия каналов для последних заказов
            recent_data = []
            for order in recent_orders:
                channel_name = "—"
                if order.slot_id:
                    slot = await session.get(Slot, order.slot_id)
                    if slot and slot.channel_id:
                        ch = await session.get(Channel, slot.channel_id)
                        if ch:
                            channel_name = ch.name or "—"
                date_str = order.paid_at.strftime("%d.%m.%y") if order.paid_at else "—"
                recent_data.append({
                    "channel": channel_name,
                    "price": float(order.final_price or 0),
                    "format": order.format_type or "—",
                    "date": date_str,
                })

        month_label = today.strftime("%m.%Y")
        text = f"📊 **Мои продажи**\n\n"

        text += f"📅 **За {month_label}:**\n"
        text += f"• Продаж: **{month_sales}**\n"
        text += f"• Выручка: **{month_revenue:,.0f}₽**\n"
        text += f"• Заработано: **{month_earned:,.0f}₽**\n\n"

        text += f"📈 **Всего:**\n"
        text += f"• Продаж: **{total_sales}**\n"
        text += f"• Выручка: **{total_revenue:,.0f}₽**\n"
        text += f"• Заработано: **{total_earned:,.0f}₽**\n"
        text += f"• Комиссия: **{commission:.0f}%**\n"

        if recent_data:
            text += f"\n💼 **Последние заказы:**\n"
            for i, o in enumerate(recent_data, 1):
                text += f"{i}. {escape_md(o['channel'])} — {o['price']:,.0f}₽ ({escape_md(o['format'])}) — {o['date']}\n"
        elif total_sales == 0:
            text += "\n_Пока нет продаж. Отправляйте реф-ссылку клиентам!_"
        
        buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_my_sales: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


# ==================== МОИ КЛИЕНТЫ ====================

@router.callback_query(F.data == "mgr_my_clients")
async def mgr_my_clients(callback: CallbackQuery):
    """Показать клиентов менеджера"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return
            
            # Получаем количество клиентов (тех, кто пришёл по реф-ссылке)
            clients_result = await session.execute(
                select(Client).where(Client.referrer_id == manager.id)
            )
            clients = clients_result.scalars().all()
            
            clients_data = []
            for client in clients:
                clients_data.append({
                    "name": client.first_name or client.username or f"ID:{client.telegram_id}",
                    "orders": 0,  # Упрощённо, без подсчёта заказов
                    "created": client.created_at.strftime("%d.%m.%Y") if client.created_at else "—"
                })
        
        text = f"👥 **Мои клиенты**\n\nВсего клиентов: **{len(clients_data)}**\n\n"
        
        if clients_data:
            for i, client in enumerate(clients_data[:15], 1):
                text += f"{i}. **{client['name']}** (с {client['created']})\n"
        else:
            text += "_Пока нет клиентов. Отправляйте реф-ссылку!_"
        
        buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_my_clients: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


# ==================== ШАБЛОНЫ ====================

@router.callback_query(F.data == "mgr_templates")
async def mgr_templates(callback: CallbackQuery):
    """Показать шаблоны сообщений"""
    await callback.answer()
    
    text = "📋 **Шаблоны для продаж**\n\n"
    text += "**🔥 Холодное сообщение:**\n"
    text += "_Привет! Хотите продвинуть свой канал/бизнес? "
    text += "У нас отличные цены на рекламу в топовых каналах! "
    text += "Напишите, расскажу подробнее._\n\n"
    
    text += "**💰 Для рекламодателей:**\n"
    text += "_Добрый день! Предлагаю размещение в качественных каналах "
    text += "с живой аудиторией. Есть разные форматы и бюджеты. "
    text += "Какая у вас ниша?_\n\n"
    
    text += "**🎯 После интереса:**\n"
    text += "_Отлично! Вот ссылка для заказа: [ваша реф-ссылка]. "
    text += "Там можно выбрать канал, дату и формат. "
    text += "Если будут вопросы — пишите!_"
    
    buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )


# ==================== РЕЙТИНГ ====================

@router.callback_query(F.data == "mgr_leaderboard")
async def mgr_leaderboard(callback: CallbackQuery):
    """Рейтинг менеджеров"""
    await callback.answer()
    
    try:
        leaderboard = await gamification_service.get_leaderboard("sales", 10)
        
        if not leaderboard:
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
            await callback.message.edit_text(
                "📊 Рейтинг пока пуст",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
            return
        
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        text = "🏆 **Рейтинг менеджеров**\n\n"
        
        for item in leaderboard:
            medal = medals.get(item["rank"], f"{item['rank']}.")
            text += f"{medal} {item['emoji']} **{item['name']}** — {item['sales']} продаж\n"
        
        buttons = [
            [
                InlineKeyboardButton(text="📦 По продажам", callback_data="lb:sales"),
                InlineKeyboardButton(text="💰 По выручке", callback_data="lb:revenue")
            ],
            [InlineKeyboardButton(text="⭐ По опыту", callback_data="lb:xp")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_leaderboard: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("lb:"))
async def leaderboard_by_metric(callback: CallbackQuery):
    """Рейтинг по выбранной метрике"""
    await callback.answer()
    
    metric = callback.data.split(":")[1]
    metric_names = {"sales": "продажам", "revenue": "выручке", "xp": "опыту"}
    
    try:
        leaderboard = await gamification_service.get_leaderboard(metric, 10)
        
        if not leaderboard:
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
            await callback.message.edit_text(
                f"📊 Рейтинг по {metric_names.get(metric, metric)} пуст",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
            return
        
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        text = f"🏆 **Рейтинг по {metric_names.get(metric, metric)}**\n\n"
        
        for item in leaderboard:
            medal = medals.get(item["rank"], f"{item['rank']}.")
            if metric == "revenue":
                value = f"{item.get('revenue', 0):,.0f}₽"
            elif metric == "xp":
                value = f"{item.get('xp', 0)} XP"
            else:
                value = f"{item.get('sales', 0)} продаж"
            text += f"{medal} {item['emoji']} **{item['name']}** — {value}\n"
        
        buttons = [
            [
                InlineKeyboardButton(text="📦 По продажам", callback_data="lb:sales"),
                InlineKeyboardButton(text="💰 По выручке", callback_data="lb:revenue")
            ],
            [InlineKeyboardButton(text="⭐ По опыту", callback_data="lb:xp")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in leaderboard_by_metric: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


# ==================== РЕФ-ССЫЛКА ====================

@router.callback_query(F.data == "copy_ref_link")
async def copy_ref_link(callback: CallbackQuery, bot: Bot):
    """Показать реферальную ссылку"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return
            
            manager_id = manager.id
        
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start=ref_{manager_id}"
        
        await callback.message.answer(
            f"🔗 **Ваша реферальная ссылка:**\n\n"
            f"`{ref_link}`\n\n"
            f"📤 Отправьте клиенту — получите комиссию с его заказа!",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in copy_ref_link: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


# ==================== ВЫВОД СРЕДСТВ ====================

@router.callback_query(F.data == "request_payout")
async def request_payout(callback: CallbackQuery, state: FSMContext):
    """Запрос на вывод средств"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return

            if not manager.is_active:
                buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
                await callback.message.edit_text(
                    "❌ Ваш аккаунт деактивирован. Обратитесь к администратору.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                )
                return

            balance = float(manager.balance or 0)
        
        if balance < 500:
            buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
            await callback.message.edit_text(
                f"❌ Минимальная сумма вывода: 500₽\n\nВаш баланс: {balance:,.0f}₽",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
            return
        
        await callback.message.edit_text(
            f"💸 **Вывод средств**\n\n"
            f"Доступно: **{balance:,.0f}₽**\n\n"
            f"Введите сумму для вывода:",
            parse_mode=ParseMode.MARKDOWN
        )
        await state.set_state(ManagerStates.payout_amount)
    except Exception as e:
        logger.error(f"Error in request_payout: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.message(ManagerStates.payout_amount)
async def receive_payout_amount(message: Message, state: FSMContext):
    """Получить сумму вывода"""
    try:
        amount = int(message.text.strip().replace(" ", ""))
    except Exception:
        await message.answer("❌ Введите число")
        return
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == message.from_user.id)
            )
            manager = result.scalar_one_or_none()
            balance = float(manager.balance or 0) if manager else 0
        
        if amount < 500:
            await message.answer("❌ Минимальная сумма: 500₽")
            return
        
        if amount > balance:
            await message.answer(f"❌ Недостаточно средств. Доступно: {balance:,.0f}₽")
            return
        
        await state.update_data(payout_amount=amount)
        
        await message.answer(
            f"💸 Сумма: **{amount:,}₽**\n\nВыберите способ получения:",
            reply_markup=get_payout_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        await state.set_state(ManagerStates.payout_method)
    except Exception as e:
        logger.error(f"Error in receive_payout_amount: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("payout:"), ManagerStates.payout_method)
async def select_payout_method(callback: CallbackQuery, state: FSMContext):
    """Выбрать способ выплаты"""
    await callback.answer()
    
    method = callback.data.split(":")[1]
    await state.update_data(payout_method=method)
    
    hints = {
        "card": "Введите номер карты (16 цифр):",
        "sbp": "Введите номер телефона для СБП (+7...):"
    }
    
    await callback.message.edit_text(hints.get(method, "Введите реквизиты:"))
    await state.set_state(ManagerStates.payout_details)


@router.message(ManagerStates.payout_details)
async def receive_payout_details(message: Message, state: FSMContext):
    """Получить реквизиты и создать заявку"""
    details = message.text.strip()
    data = await state.get_data()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == message.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await message.answer("❌ Ошибка")
                await state.clear()
                return
            
            amount = data.get("payout_amount", 0)
            method = data.get("payout_method", "card")
            
            # Создаём заявку
            payout = ManagerPayout(
                manager_id=manager.id,
                amount=Decimal(str(amount)),
                method=method,
                details=details,
                status="pending"
            )
            session.add(payout)
            
            # Списываем с баланса
            manager.balance -= Decimal(str(amount))
            
            await session.commit()
        
        await state.clear()
        
        await message.answer(
            f"✅ **Заявка на вывод создана!**\n\n"
            f"💸 Сумма: {amount:,}₽\n"
            f"📱 Способ: {method}\n"
            f"📋 Реквизиты: {details}\n\n"
            f"Ожидайте обработки в течение 24 часов.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in receive_payout_details: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")
        await state.clear()


# ==================== ИСТОРИЯ ВЫПЛАТ ====================

@router.callback_query(F.data == "payout_history")
async def payout_history(callback: CallbackQuery):
    """История выплат"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return
            
            payouts_result = await session.execute(
                select(ManagerPayout)
                .where(ManagerPayout.manager_id == manager.id)
                .order_by(ManagerPayout.created_at.desc())
                .limit(10)
            )
            payouts = payouts_result.scalars().all()
            
            payouts_data = [{
                "amount": float(p.amount),
                "status": p.status,
                "date": p.created_at.strftime("%d.%m.%Y") if p.created_at else "—"
            } for p in payouts]
        
        text = "💸 **История выплат**\n\n"
        
        if payouts_data:
            for p in payouts_data:
                status_emoji = {"pending": "⏳", "completed": "✅", "rejected": "❌"}.get(p["status"], "❓")
                text += f"{status_emoji} {p['amount']:,.0f}₽ — {p['date']}\n"
        else:
            text += "_Выплат пока не было_"
        
        buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in payout_history: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


# ==================== МОИ ПОСТЫ ====================

@router.callback_query(F.data == "mgr_my_posts")
async def mgr_my_posts(callback: CallbackQuery):
    """Показать посты, поданные менеджером"""
    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledPost)
                .where(ScheduledPost.created_by == callback.from_user.id)
                .order_by(ScheduledPost.created_at.desc())
                .limit(15)
            )
            posts = result.scalars().all()

            manager_result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = manager_result.scalar_one_or_none()
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return

            posts_data = []
            for post in posts:
                channel = await session.get(Channel, post.channel_id)
                posts_data.append({
                    "id": post.id,
                    "channel": channel.name if channel else "—",
                    "status": post.status,
                    "scheduled_time": post.scheduled_time,
                })

        mgr_tz_offset, mgr_tz_label = _manager_tz(manager)

        status_labels = {
            "moderation": "🔍 На модерации",
            "pending": "⏳ В очереди",
            "posted": "✅ Опубликован",
            "rejected": "❌ Отклонён",
            "cancelled": "🚫 Отменён",
            "error": "⚠️ Ошибка",
        }

        if not posts_data:
            text = "📋 **Мои посты**\n\nУ вас ещё нет поданных постов."
        else:
            text = f"📋 **Мои посты** ({len(posts_data)})\n\n"
            for p in posts_data:
                status_label = status_labels.get(p["status"], p["status"])
                sched = (
                    (p["scheduled_time"] + mgr_tz_offset).strftime("%d.%m %H:%M") + f" {mgr_tz_label}"
                    if p["scheduled_time"] else "—"
                )
                text += f"#{p['id']} | {escape_md(p['channel'])} | {sched}\n{status_label}\n\n"

        buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_my_posts: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


# ==================== ПОДАЧА ПОСТА НА МОДЕРАЦИЮ ====================

@router.callback_query(F.data == "mgr_submit_post")
async def mgr_submit_post_start(callback: CallbackQuery, state: FSMContext):
    """Начало подачи поста: выбор канала"""
    await callback.answer()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return

            channels_result = await session.execute(
                select(Channel).where(Channel.is_active == True)
            )
            channels = channels_result.scalars().all()
            channels_data = [{"id": ch.id, "name": ch.name, "prices": ch.prices or {}} for ch in channels]

        if not channels_data:
            await callback.message.edit_text(
                "😔 Нет доступных каналов для размещения.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]
                ])
            )
            return

        buttons = []
        for ch in channels_data:
            price_124 = ch["prices"].get("1/24", 0)
            buttons.append([InlineKeyboardButton(
                text=f"📢 {ch['name']} — от {price_124:,}₽",
                callback_data=f"mgr_submit_post:{ch['id']}"
            )])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")])

        await callback.message.edit_text(
            "📝 **Подача поста на модерацию**\n\nВыберите канал для размещения:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_submit_post_start: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("mgr_submit_post:"))
async def mgr_submit_post_channel(callback: CallbackQuery, state: FSMContext):
    """Выбран канал: показать доступные даты"""
    await callback.answer()

    channel_id = int(callback.data.split(":")[1])

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                return

            channel = await session.get(Channel, channel_id)
            if not channel:
                await callback.message.answer("❌ Канал не найден")
                return

            slots_result = await session.execute(
                select(Slot).where(
                    Slot.channel_id == channel_id,
                    Slot.status == "available",
                    Slot.slot_date >= date.today()
                ).order_by(Slot.slot_date)
            )
            slots = slots_result.scalars().all()

        if not slots:
            await callback.message.edit_text(
                f"😔 Нет доступных слотов в канале **{channel.name}**.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Выбрать другой канал", callback_data="mgr_submit_post")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
            return

        await state.update_data(
            mgr_channel_id=channel_id,
            mgr_channel_name=channel.name,
            mgr_prices=channel.prices or {}
        )

        await callback.message.edit_text(
            f"📢 **{channel.name}**\n\n📅 Выберите дату размещения:",
            reply_markup=get_calendar_keyboard(
                slots,
                date.today().year,
                date.today().month,
                back_cb="mgr_submit_post",
                date_cb_prefix="mgr_post_date",
                nav_cb_prefix="mgr_cal_nav",
            ),
            parse_mode=ParseMode.MARKDOWN
        )
        await state.set_state(ManagerPostStates.selecting_date)
    except Exception as e:
        logger.error(f"Error in mgr_submit_post_channel: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("mgr_cal_nav:"), ManagerPostStates.selecting_date)
async def mgr_cal_nav(callback: CallbackQuery, state: FSMContext):
    """Навигация по месяцам в календаре менеджера"""
    await callback.answer()

    try:
        _, year_s, month_s = callback.data.split(":")
        year, month = int(year_s), int(month_s)
    except (ValueError, IndexError):
        logger.warning(f"mgr_cal_nav: malformed callback data: {callback.data!r}")
        return

    data = await state.get_data()
    channel_id = data.get("mgr_channel_id")
    if not channel_id:
        return

    try:
        async with async_session_maker() as session:
            slots_result = await session.execute(
                select(Slot).where(
                    Slot.channel_id == channel_id,
                    Slot.status == "available",
                    Slot.slot_date >= date.today()
                )
            )
            slots = slots_result.scalars().all()

        await callback.message.edit_reply_markup(
            reply_markup=get_calendar_keyboard(
                slots,
                year,
                month,
                back_cb="mgr_submit_post",
                date_cb_prefix="mgr_post_date",
                nav_cb_prefix="mgr_cal_nav",
            )
        )
    except Exception as e:
        logger.error(f"Error in mgr_cal_nav: {traceback.format_exc()}")


@router.callback_query(F.data.startswith("mgr_post_date:"))
async def mgr_post_select_date(callback: CallbackQuery, state: FSMContext):
    """Выбор даты: показать доступные слоты времени"""
    await callback.answer()

    date_str = callback.data.split(":")[1]
    selected_date = date.fromisoformat(date_str)

    data = await state.get_data()
    channel_id = data.get("mgr_channel_id")
    channel_name = data.get("mgr_channel_name", "Канал")

    try:
        async with async_session_maker() as session:
            slots_result = await session.execute(
                select(Slot).where(
                    Slot.channel_id == channel_id,
                    Slot.slot_date == selected_date,
                    Slot.status == "available"
                ).order_by(Slot.slot_time)
            )
            slots = slots_result.scalars().all()

            # Получаем timezone менеджера
            mgr_result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = mgr_result.scalar_one_or_none()

        if not slots:
            await callback.message.edit_text("😔 На эту дату нет доступных слотов.")
            return

        await state.update_data(mgr_selected_date=date_str)

        mgr_tz_offset, mgr_tz_label = _manager_tz(manager)

        buttons = []
        for slot in slots:
            # Конвертируем время слота (в timezone бота) → UTC → timezone менеджера
            slot_dt = datetime.combine(selected_date, slot.slot_time)
            slot_utc = slot_dt - LOCAL_TZ_OFFSET
            slot_local = slot_utc + mgr_tz_offset
            time_str = slot_local.strftime("%H:%M")
            buttons.append([InlineKeyboardButton(
                text=f"🕐 {time_str}",
                callback_data=f"mgr_post_time:{slot.id}"
            )])
        buttons.append([InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=f"mgr_submit_post:{channel_id}"
        )])

        await callback.message.edit_text(
            f"📢 **{channel_name}**\n"
            f"📅 {selected_date.strftime('%d.%m.%Y')}\n\n"
            f"🕐 Выберите время ({mgr_tz_label}):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
        await state.set_state(ManagerPostStates.selecting_time)
    except Exception as e:
        logger.error(f"Error in mgr_post_select_date: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data.startswith("mgr_post_time:"))
async def mgr_post_select_time(callback: CallbackQuery, state: FSMContext):
    """Выбор времени: предложить формат размещения"""
    await callback.answer()

    slot_id = int(callback.data.split(":")[1])

    data = await state.get_data()
    channel_name = data.get("mgr_channel_name", "Канал")
    prices = data.get("mgr_prices", {})
    selected_date = data.get("mgr_selected_date", "")

    await state.update_data(mgr_slot_id=slot_id)

    format_names = {
        "1/24": f"1/24 (24ч) — {prices.get('1/24', 0):,}₽",
        "1/48": f"1/48 (48ч) — {prices.get('1/48', 0):,}₽",
        "2/48": f"2/48 (2 поста) — {prices.get('2/48', 0):,}₽",
        "native": f"Навсегда — {prices.get('native', 0):,}₽",
    }

    buttons = []
    for fmt, label in format_names.items():
        buttons.append([InlineKeyboardButton(
            text=label,
            callback_data=f"mgr_post_format:{fmt}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"mgr_post_date:{selected_date}")])

    await callback.message.edit_text(
        f"📢 **{channel_name}**\n"
        f"📅 {selected_date}\n\n"
        f"📋 Выберите формат размещения:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerPostStates.selecting_format)


@router.callback_query(F.data.startswith("mgr_post_format:"))
async def mgr_post_select_format(callback: CallbackQuery, state: FSMContext):
    """Выбор формата: запросить рекламный контент"""
    await callback.answer()

    format_type = callback.data.split(":")[1]

    data = await state.get_data()
    channel_name = data.get("mgr_channel_name", "Канал")
    prices = data.get("mgr_prices", {})
    selected_date = data.get("mgr_selected_date", "")
    price = prices.get(format_type, 0)

    format_labels = {
        "1/24": "1/24 (24 часа)",
        "1/48": "1/48 (48 часов)",
        "2/48": "2/48 (2 поста за 48ч)",
        "native": "Навсегда"
    }

    await state.update_data(mgr_format_type=format_type, mgr_price=price)

    await callback.message.edit_text(
        f"📝 **Отправьте рекламный материал**\n\n"
        f"📢 Канал: {channel_name}\n"
        f"📅 Дата: {selected_date}\n"
        f"📋 Формат: {format_labels.get(format_type, format_type)}\n"
        f"💰 Цена: **{price:,}₽**\n\n"
        f"Отправьте текст поста (можно с фото или видео):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="mgr_back")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerPostStates.entering_content)


@router.message(ManagerPostStates.entering_content)
async def mgr_post_receive_content(message: Message, state: FSMContext):
    """Получение рекламного контента от менеджера"""
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
            "❌ Отправьте текст поста или медиафайл (фото/видео).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="mgr_back")]
            ])
        )
        return

    await state.update_data(
        mgr_ad_content=content_text,
        mgr_ad_file_id=file_id,
        mgr_ad_file_type=file_type
    )

    data = await state.get_data()
    channel_name = data.get("mgr_channel_name", "Канал")
    await message.answer(
        "✍️ **Подпись поста** (необязательно)\n\n"
        f"Нажмите **Автоподпись**, чтобы добавить в пост кликабельную ссылку на канал "
        f"с текстом «{channel_name}».\n\n"
        "Или введите:\n"
        "• Просто текст — подпись без ссылки\n"
        "• `Текст | https://ссылка` — кликабельная подпись с вашей ссылкой в теле поста\n\n"
        "Либо пропустите этот шаг.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Автоподпись", callback_data="mgr_auto_signature")],
            [InlineKeyboardButton(text="➡️ Без подписи", callback_data="mgr_signature_skip")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="mgr_back")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )
    await state.set_state(ManagerPostStates.entering_signature)


async def _mgr_show_confirm(message: Message, state: FSMContext) -> None:
    """Показать сводку поста и запросить подтверждение отправки на модерацию."""
    data = await state.get_data()
    channel_name = data.get("mgr_channel_name", "Канал")
    format_type = data.get("mgr_format_type", "1/24")
    price = data.get("mgr_price", 0)
    selected_date = data.get("mgr_selected_date", "")
    content_text = data.get("mgr_ad_content", "")
    file_id = data.get("mgr_ad_file_id")
    file_type = data.get("mgr_ad_file_type")
    signature = data.get("mgr_ad_signature")

    format_labels = {
        "1/24": "1/24 (24 часа)",
        "1/48": "1/48 (48 часов)",
        "2/48": "2/48 (2 поста за 48ч)",
        "native": "Навсегда"
    }

    text = (
        f"✅ **Подтверждение подачи поста**\n\n"
        f"📢 Канал: {channel_name}\n"
        f"📅 Дата: {selected_date}\n"
        f"📋 Формат: {format_labels.get(format_type, format_type)}\n"
        f"💰 Цена: **{price:,}₽**\n\n"
    )
    if content_text:
        text += f"📝 Текст:\n{content_text[:300]}{'...' if len(content_text) > 300 else ''}\n\n"
    if file_id:
        text += f"📎 Медиафайл: {file_type}\n\n"
    if signature:
        text += f"✍️ Подпись: {signature}\n\n"
    text += "Отправить пост на модерацию администратору?"

    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Отправить на модерацию", callback_data="mgr_post_confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="mgr_back")
            ]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerPostStates.confirming)


@router.callback_query(F.data == "mgr_auto_signature", ManagerPostStates.entering_signature)
async def mgr_post_auto_signature(callback: CallbackQuery, state: FSMContext):
    """Установка автоподписи (название канала) и переход к подтверждению"""
    await callback.answer()
    data = await state.get_data()
    channel_name = data.get("mgr_channel_name", "Реклама")
    await state.update_data(mgr_ad_signature=channel_name)
    await _mgr_show_confirm(callback.message, state)


@router.callback_query(F.data == "mgr_signature_skip", ManagerPostStates.entering_signature)
async def mgr_post_signature_skip(callback: CallbackQuery, state: FSMContext):
    """Пропуск подписи и переход к подтверждению"""
    await callback.answer()
    await state.update_data(mgr_ad_signature=None)
    await _mgr_show_confirm(callback.message, state)


@router.message(ManagerPostStates.entering_signature)
async def mgr_post_signature_enter(message: Message, state: FSMContext):
    """Сохранение пользовательского текста подписи для менеджера"""
    signature_text = (message.text or "").strip()
    if not signature_text:
        await message.answer(
            "❌ Введите текст подписи или нажмите **Без подписи**.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Валидация формата «Текст | URL» если указан разделитель
    if " | " in signature_text:
        parts = signature_text.split(" | ", 1)
        sig_url = parts[1].strip()
        if not sig_url.startswith(("http://", "https://", "tg://")):
            await message.answer(
                "❌ Неверный формат ссылки. Ссылка должна начинаться с `http://`, `https://` или `tg://`.\n\n"
                "Формат: `Текст | https://ссылка`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    await state.update_data(mgr_ad_signature=signature_text)
    await _mgr_show_confirm(message, state)


@router.callback_query(F.data == "mgr_post_confirm", ManagerPostStates.confirming)
async def mgr_post_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Запрос скрина оплаты перед отправкой поста на модерацию"""
    await callback.answer()

    payment_link = await get_setting(PAYMENT_LINK_KEY)
    if payment_link:
        safe_link = payment_link.replace("`", "'")
        payment_info = f"\n💳 **Реквизиты для оплаты:**\n`{safe_link}`\n"
    else:
        payment_info = ""

    await callback.message.edit_text(
        f"💳 **Загрузите скриншот оплаты**\n{payment_info}\n"
        "Оплатите размещение и отправьте фото или документ с подтверждением перевода.\n\n"
        "После проверки скриншота ваш пост будет передан на модерацию.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="mgr_back")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerPostStates.uploading_payment)


@router.message(ManagerPostStates.uploading_payment)
async def mgr_post_receive_payment(message: Message, state: FSMContext, bot: Bot):
    """Получение скриншота оплаты и создание поста на модерации"""
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id

    if not file_id:
        await message.answer("❌ Отправьте фото или документ с подтверждением оплаты.")
        return

    data = await state.get_data()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == message.from_user.id)
            )
            manager = result.scalar_one_or_none()
            if not manager:
                await message.answer("❌ Вы не менеджер")
                await state.clear()
                return

            slot_id = data.get("mgr_slot_id")
            slot = await session.get(Slot, slot_id)
            if not slot or slot.status != "available":
                await message.answer(
                    "❌ Выбранный слот уже недоступен. Попробуйте снова.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]
                    ])
                )
                await state.clear()
                return

            channel_id = data.get("mgr_channel_id")
            selected_date_str = data.get("mgr_selected_date", "")
            slot_dt = datetime.combine(date.fromisoformat(selected_date_str), slot.slot_time)
            scheduled_time = slot_dt - LOCAL_TZ_OFFSET  # UTC

            is_owner = OWNER_ID is not None and message.from_user.id == OWNER_ID
            post = ScheduledPost(
                channel_id=channel_id,
                content=data.get("mgr_ad_content", ""),
                file_id=data.get("mgr_ad_file_id"),
                file_type=data.get("mgr_ad_file_type"),
                signature=data.get("mgr_ad_signature") or None,
                scheduled_time=scheduled_time,
                delete_after_hours=FORMAT_DELETE_HOURS.get(
                    data.get("mgr_format_type", "1/24"), 24
                ),
                status="pending" if is_owner else "moderation",
                created_by=message.from_user.id,
                payment_screenshot=file_id,
            )
            session.add(post)
            await session.commit()
            post_id = post.id

        await state.clear()

        mgr_tz_offset, mgr_tz_label = _manager_tz(manager)
        scheduled_local = scheduled_time + mgr_tz_offset

        if is_owner:
            await message.answer(
                f"✅ **Пост #{post_id} поставлен в очередь!**\n\n"
                f"📅 Время публикации: **{scheduled_local.strftime('%d.%m.%Y %H:%M')} {mgr_tz_label}**\n\n"
                f"Пост будет автоматически опубликован в указанное время.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Ещё пост для этого канала", callback_data=f"mgr_submit_post:{channel_id}")],
                    [InlineKeyboardButton(text="◀️ В кабинет", callback_data="mgr_back")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await message.answer(
                f"✅ **Скриншот получен! Пост #{post_id} отправлен на модерацию.**\n\n"
                f"📅 Время публикации: **{scheduled_local.strftime('%d.%m.%Y %H:%M')} {mgr_tz_label}**\n\n"
                f"Администратор проверит оплату и рассмотрит пост в ближайшее время.\n"
                f"После одобрения пост будет автоматически опубликован в указанное время.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Ещё пост для этого канала", callback_data=f"mgr_submit_post:{channel_id}")],
                    [InlineKeyboardButton(text="◀️ В кабинет", callback_data="mgr_back")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )

            # Уведомляем администраторов (время в timezone бота)
            scheduled_admin = scheduled_time + LOCAL_TZ_OFFSET
            channel_name_notify = data.get("mgr_channel_name") or str(channel_id)
            for admin_id in ADMIN_IDS:
                try:
                    caption = (
                        f"📝 **Новый пост на модерацию #{post_id}**\n\n"
                        f"От менеджера: {message.from_user.first_name or message.from_user.username}\n"
                        f"📢 Канал: {channel_name_notify}\n"
                        f"📅 Запланирован: {scheduled_admin.strftime('%d.%m.%Y %H:%M')} {LOCAL_TZ_LABEL}\n\n"
                        f"💳 Скриншот оплаты прикреплён."
                    )
                    moderation_markup = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔍 Перейти к модерации", callback_data=f"adm_post:{post_id}")]
                    ])
                    if message.photo:
                        await bot.send_photo(
                            admin_id,
                            file_id,
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=moderation_markup,
                        )
                    else:
                        await bot.send_document(
                            admin_id,
                            file_id,
                            caption=caption,
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=moderation_markup,
                        )
                except Exception:
                    logger.warning(f"Could not notify admin {admin_id} about new post #{post_id}", exc_info=True)

    except Exception as e:
        logger.error(f"Error in mgr_post_receive_payment: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка: {str(e)[:200]}")
        await state.clear()

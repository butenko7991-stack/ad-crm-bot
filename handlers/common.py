"""
Общие обработчики (start, help, текстовые кнопки)
"""
import logging
import traceback

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, CommandStart
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from config import ADMIN_IDS, MANAGER_LEVELS
from database import async_session_maker, Manager, Client, Channel, Order
from keyboards import (
    get_main_menu, get_admin_panel_menu, get_manager_cabinet_menu, 
    get_channels_keyboard, get_training_menu
)
from handlers.admin import authenticated_admins


logger = logging.getLogger(__name__)
router = Router()


# ==================== КОМАНДА /START ====================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Команда /start"""
    await state.clear()
    
    user = message.from_user
    user_id = user.id
    
    # Проверяем реферальную ссылку
    args = message.text.split()
    ref_manager_id = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_manager_id = int(args[1].replace("ref_", ""))
        except:
            pass
    
    is_admin = user_id in ADMIN_IDS
    
    # Проверяем, является ли менеджером
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == user_id)
        )
        manager = result.scalar_one_or_none()
        
        # Если пришёл по рефке — сохраняем привязку
        if ref_manager_id:
            client_result = await session.execute(
                select(Client).where(Client.telegram_id == user_id)
            )
            client = client_result.scalar_one_or_none()
            
            if not client:
                client = Client(
                    telegram_id=user_id,
                    username=user.username,
                    first_name=user.first_name
                )
                session.add(client)
                await session.commit()
    
    if manager:
        level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
        await message.answer(
            f"👋 **С возвращением, {manager.first_name}!**\n\n"
            f"{level_info['emoji']} Уровень: {level_info['name']}\n"
            f"💰 Баланс: **{float(manager.balance):,.0f}₽**\n"
            f"📦 Продаж: {manager.total_sales}",
            reply_markup=get_main_menu(is_admin=is_admin, is_manager=True),
            parse_mode=ParseMode.MARKDOWN
        )
    elif is_admin:
        await message.answer(
            "👋 **Добро пожаловать, Администратор!**\n\n"
            "Нажмите кнопку для входа в админ-панель.",
            reply_markup=get_main_menu(is_admin=True, is_manager=False),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.answer(
            "👋 **Добро пожаловать!**\n\n"
            "Здесь вы можете забронировать рекламу в наших Telegram-каналах.\n\n"
            "📢 Выберите канал из каталога\n"
            "📅 Забронируйте дату и время\n"
            "💰 Оплатите размещение\n"
            "✅ Получите результат!",
            reply_markup=get_main_menu(is_admin=False, is_manager=False),
            parse_mode=ParseMode.MARKDOWN
        )


# ==================== КОМАНДЫ ====================

@router.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help"""
    await message.answer(
        "📚 **Справка**\n\n"
        "**Основные команды:**\n"
        "/start — Главное меню\n"
        "/catalog — Каталог каналов\n"
        "/orders — Мои заказы\n\n"
        "**Для менеджеров:**\n"
        "/manager — Кабинет менеджера\n"
        "/training — Обучение\n"
        "/sales — Каналы для продажи\n\n"
        "**Для админов:**\n"
        "/admin — Админ-панель",
        parse_mode=ParseMode.MARKDOWN
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Команда /admin"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ У вас нет доступа к админ-панели")
        return
    
    if message.from_user.id in authenticated_admins:
        await message.answer(
            "⚙️ **Админ-панель**\n\nВыберите действие:",
            reply_markup=get_admin_panel_menu(),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.answer(
            "🔐 **Вход в админ-панель**\n\nНажмите кнопку для авторизации:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔑 Войти", callback_data="request_admin_password")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )


@router.message(Command("manager"))
async def cmd_manager(message: Message):
    """Команда /manager — кабинет менеджера"""
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if not manager:
        await message.answer(
            "❌ Вы не зарегистрированы как менеджер.\n\n"
            "Нажмите кнопку **💼 Стать менеджером** в главном меню.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
    
    await message.answer(
        f"👤 **Кабинет менеджера**\n\n"
        f"{level_info['emoji']} {manager.first_name}\n"
        f"📊 Уровень: **{level_info['name']}**\n"
        f"💰 Баланс: **{float(manager.balance):,.0f}₽**\n"
        f"📦 Продаж: {manager.total_sales}\n"
        f"💵 Выручка: {float(manager.total_revenue):,.0f}₽",
        reply_markup=get_manager_cabinet_menu(),
        parse_mode=ParseMode.MARKDOWN
    )


@router.message(Command("catalog"))
async def cmd_catalog(message: Message):
    """Команда /catalog — каталог каналов"""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Channel).where(Channel.is_active == True)
            )
            channels = result.scalars().all()
            
            channels_data = [{
                "id": ch.id,
                "name": ch.name,
                "prices": ch.prices or {}
            } for ch in channels]
        
        if not channels_data:
            await message.answer("😔 Каналов пока нет")
            return
        
        await message.answer(
            "📢 **Каталог каналов**\n\nВыберите канал:",
            reply_markup=get_channels_keyboard(channels_data),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in cmd_catalog: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.message(Command("training"))
async def cmd_training(message: Message):
    """Команда /training — обучение"""
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if not manager:
        await message.answer("❌ Обучение доступно только менеджерам")
        return
    
    await message.answer(
        "📚 **Обучение менеджера**\n\nВыберите раздел:",
        reply_markup=get_training_menu(),
        parse_mode=ParseMode.MARKDOWN
    )


# ==================== ТЕКСТОВЫЕ КНОПКИ ====================

@router.message(F.text == "📢 Каталог каналов")
async def btn_catalog(message: Message):
    """Кнопка каталога"""
    await cmd_catalog(message)


@router.message(F.text == "📦 Мои заказы")
async def btn_my_orders(message: Message):
    """Кнопка мои заказы"""
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Client).where(Client.telegram_id == message.from_user.id)
            )
            client = result.scalar_one_or_none()
            
            if not client:
                await message.answer("📦 У вас пока нет заказов")
                return
            
            orders_result = await session.execute(
                select(Order)
                .where(Order.client_id == client.id)
                .order_by(Order.created_at.desc())
                .limit(10)
            )
            orders = orders_result.scalars().all()
        
        if not orders:
            await message.answer("📦 У вас пока нет заказов")
            return
        
        text = "📦 **Ваши заказы:**\n\n"
        
        status_names = {
            "pending": "⏳ Ожидает оплаты",
            "payment_uploaded": "📤 Оплата на проверке",
            "payment_confirmed": "✅ Оплачен",
            "posted": "📝 Опубликован",
            "completed": "✔️ Завершён"
        }
        
        for order in orders:
            status = status_names.get(order.status, "❓ " + order.status)
            text += f"• Заказ #{order.id} — {float(order.final_price):,.0f}₽ — {status}\n"
        
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in btn_my_orders: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.message(F.text == "💼 Стать менеджером")
async def btn_become_manager(message: Message):
    """Кнопка стать менеджером"""
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        existing = result.scalar_one_or_none()
    
    if existing:
        await message.answer("✅ Вы уже зарегистрированы как менеджер!\n\nИспользуйте /manager")
        return
    
    await message.answer(
        "💼 **Стать менеджером**\n\n"
        "Зарабатывайте на продаже рекламы!\n\n"
        "**Условия:**\n"
        "💰 Комиссия 10-25% от каждой продажи\n"
        "📚 Бесплатное обучение\n"
        "🏆 Бонусы за достижения\n"
        "📈 Карьерный рост",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Стать менеджером", callback_data="manager_register")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )


@router.message(F.text == "🔐 Войти в админку")
async def btn_admin_login(message: Message):
    """Кнопка входа в админку"""
    await cmd_admin(message)


@router.message(F.text == "👤 Профиль")
async def btn_profile(message: Message):
    """Кнопка профиля менеджера"""
    await cmd_manager(message)


@router.message(F.text == "📚 Обучение")
async def btn_training(message: Message):
    """Кнопка обучения"""
    await cmd_training(message)


@router.message(F.text == "💼 Продажи")
async def btn_sales(message: Message):
    """Кнопка продаж"""
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
        
        if not manager:
            await message.answer("❌ Вы не менеджер")
            return
        
        result = await session.execute(
            select(Channel).where(Channel.is_active == True)
        )
        channels = result.scalars().all()
        
        channels_data = [{
            "id": ch.id,
            "name": ch.name,
            "prices": ch.prices or {}
        } for ch in channels]
    
    if not channels_data:
        await message.answer("😔 Каналов пока нет")
        return
    
    text = "💼 **Каналы для продажи:**\n\n"
    buttons = []
    
    for ch in channels_data:
        prices = ch["prices"]
        price_124 = prices.get("1/24", 0)
        text += f"📢 **{ch['name']}** — от {price_124:,}₽\n"
        buttons.append([InlineKeyboardButton(
            text=f"📊 {ch['name']}",
            callback_data=f"analyze_ch:{ch['id']}"
        )])
    
    buttons.append([InlineKeyboardButton(text="📋 Моя реф-ссылка", callback_data="copy_ref_link")])
    
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )


@router.message(F.text == "🏆 Рейтинг")
async def btn_leaderboard(message: Message):
    """Кнопка рейтинга"""
    from services import gamification_service
    
    try:
        leaderboard = await gamification_service.get_leaderboard("sales", 10)
        
        if not leaderboard:
            await message.answer("📊 Рейтинг пока пуст")
            return
        
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        text = "🏆 **Рейтинг менеджеров**\n\n"
        
        for item in leaderboard:
            medal = medals.get(item["rank"], f"{item['rank']}.")
            text += f"{medal} {item['emoji']} **{item['name']}** — {item['sales']} продаж\n"
        
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Error in btn_leaderboard: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)

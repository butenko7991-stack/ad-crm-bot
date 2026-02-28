"""
Обработчики для менеджеров
"""
import logging
import traceback
from datetime import datetime
from decimal import Decimal

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from config import MANAGER_LEVELS, CHANNEL_CATEGORIES
from database import async_session_maker, Manager, Order, Client, Channel, ManagerPayout
from keyboards import get_manager_cabinet_menu, get_payout_keyboard, get_training_menu
from utils import ManagerStates
from services import gamification_service


logger = logging.getLogger(__name__)
router = Router()


# ==================== РЕГИСТРАЦИЯ МЕНЕДЖЕРА ====================

@router.callback_query(F.data == "manager_register")
async def manager_register(callback: CallbackQuery):
    """Регистрация нового менеджера"""
    await callback.answer()
    
    user = callback.from_user
    
    try:
        async with async_session_maker() as session:
            # Проверяем, не зарегистрирован ли уже
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
            
            # Создаём менеджера
            manager = Manager(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name or user.username or "Менеджер",
                status="trainee",
                level=1,
                commission_rate=Decimal("10")
            )
            session.add(manager)
            await session.commit()
        
        await callback.message.edit_text(
            "🎉 **Добро пожаловать в команду!**\n\n"
            "Вы успешно зарегистрированы как менеджер.\n\n"
            "**Что дальше:**\n"
            "📚 Пройдите обучение — /training\n"
            "💼 Начните продавать — /sales\n"
            "💰 Получайте комиссию 10-25%\n\n"
            "Нажмите /manager для входа в кабинет.",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in manager_register: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка регистрации:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== КАБИНЕТ МЕНЕДЖЕРА ====================

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
            
            level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
            name = manager.first_name or "Менеджер"
            balance = float(manager.balance or 0)
            total_sales = manager.total_sales or 0
        
        await callback.message.edit_text(
            f"👤 **Кабинет менеджера**\n\n"
            f"{level_info['emoji']} {name}\n"
            f"📊 Уровень: **{level_info['name']}**\n"
            f"💰 Баланс: **{balance:,.0f}₽**\n"
            f"📦 Продаж: {total_sales}",
            reply_markup=get_manager_cabinet_menu(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_back: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
            
            # Сохраняем данные
            total_sales = manager.total_sales or 0
            total_revenue = float(manager.total_revenue or 0)
            total_earned = float(manager.total_earned or 0)
            
            # Получаем заказы
            orders_result = await session.execute(
                select(Order)
                .where(Order.manager_id == manager.id)
                .order_by(Order.created_at.desc())
                .limit(10)
            )
            orders = orders_result.scalars().all()
            
            orders_data = [{
                "id": o.id,
                "status": o.status,
                "price": float(o.final_price or 0)
            } for o in orders]
        
        text = f"📊 **Мои продажи**\n\n"
        text += f"Всего продаж: **{total_sales}**\n"
        text += f"Общая выручка: **{total_revenue:,.0f}₽**\n"
        text += f"Мой заработок: **{total_earned:,.0f}₽**\n\n"
        
        if orders_data:
            text += "**Последние заказы:**\n"
            for order in orders_data:
                status_emoji = {"payment_confirmed": "✅", "pending": "⏳"}.get(order["status"], "❓")
                text += f"{status_emoji} #{order['id']} — {order['price']:,.0f}₽\n"
        else:
            text += "_Пока нет заказов_"
        
        buttons = [[InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in mgr_my_sales: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
            
            # Получаем заказы
            orders_result = await session.execute(
                select(Order).where(Order.manager_id == manager.id)
            )
            orders = orders_result.scalars().all()
            
            # Собираем клиентов
            client_ids = set()
            clients_data = []
            
            for order in orders:
                if order.client_id not in client_ids:
                    client_ids.add(order.client_id)
                    client = await session.get(Client, order.client_id)
                    if client:
                        client_orders = [o for o in orders if o.client_id == client.id]
                        total_spent = sum(float(o.final_price or 0) for o in client_orders)
                        clients_data.append({
                            "name": client.first_name or client.username or f"ID:{client.telegram_id}",
                            "orders": len(client_orders),
                            "spent": total_spent
                        })
        
        text = f"👥 **Мои клиенты**\n\nВсего клиентов: **{len(clients_data)}**\n\n"
        
        if clients_data:
            clients_data.sort(key=lambda x: x["spent"], reverse=True)
            for i, client in enumerate(clients_data[:15], 1):
                text += f"{i}. **{client['name']}**\n"
                text += f"   📦 {client['orders']} заказов | 💰 {client['spent']:,.0f}₽\n\n"
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
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== РЕЙТИНГ ====================

@router.callback_query(F.data == "mgr_leaderboard")
async def mgr_leaderboard(callback: CallbackQuery):
    """Рейтинг менеджеров"""
    await callback.answer()
    
    try:
        leaderboard = await gamification_service.get_leaderboard("sales", 10)
        
        if not leaderboard:
            await callback.message.edit_text("📊 Рейтинг пока пуст")
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
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data.startswith("lb:"))
async def leaderboard_by_metric(callback: CallbackQuery):
    """Рейтинг по выбранной метрике"""
    await callback.answer()
    
    metric = callback.data.split(":")[1]
    metric_names = {"sales": "продажам", "revenue": "выручке", "xp": "опыту"}
    
    try:
        leaderboard = await gamification_service.get_leaderboard(metric, 10)
        
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        text = f"🏆 **Рейтинг по {metric_names.get(metric, metric)}**\n\n"
        
        for item in leaderboard:
            medal = medals.get(item["rank"], f"{item['rank']}.")
            if metric == "revenue":
                value = f"{item['revenue']:,.0f}₽"
            elif metric == "xp":
                value = f"{item['xp']} XP"
            else:
                value = f"{item['sales']} продаж"
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
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
            
            balance = float(manager.balance or 0)
        
        if balance < 500:
            await callback.message.edit_text(
                f"❌ Минимальная сумма вывода: 500₽\n\nВаш баланс: {balance:,.0f}₽"
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
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.message(ManagerStates.payout_amount)
async def receive_payout_amount(message: Message, state: FSMContext):
    """Получить сумму вывода"""
    try:
        amount = int(message.text.strip().replace(" ", ""))
    except:
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
        await message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


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
        await message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)
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
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)

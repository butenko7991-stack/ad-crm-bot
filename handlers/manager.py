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

from config import MANAGER_LEVELS, CHANNEL_CATEGORIES, ADMIN_IDS
from database import async_session_maker, Manager, Order, Client, Channel, ManagerPayout, Slot, ScheduledPost
from keyboards import get_manager_cabinet_menu, get_payout_keyboard, get_training_menu
from utils import ManagerStates, ManagerPostStates
from services import gamification_service


logger = logging.getLogger(__name__)
router = Router()

# Часы удаления поста по формату размещения
FORMAT_DELETE_HOURS = {
    "1/24": 24,
    "1/48": 48,
    "2/48": 48,
    "native": 0,
}


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
        await callback.message.answer(f"❌ Ошибка регистрации: {str(e)[:100]}")


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
            
            text = f"📢 **{channel.name}**\n\n"
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
            text += f"📢 **{ch['name']}** — от {price_124:,}₽\n"
            buttons.append([InlineKeyboardButton(
                text=f"📊 {ch['name']}",
                callback_data=f"analyze_ch:{ch['id']}"
            )])
        
        buttons.append([InlineKeyboardButton(text="📋 Моя реф-ссылка", callback_data="copy_ref_link")])
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in back_to_sales: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


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
            manager_id = manager.id
        
        text = f"📊 **Мои продажи**\n\n"
        text += f"Всего продаж: **{total_sales}**\n"
        text += f"Общая выручка: **{total_revenue:,.0f}₽**\n"
        text += f"Мой заработок: **{total_earned:,.0f}₽**\n\n"
        
        if total_sales == 0:
            text += "_Пока нет продаж. Отправляйте реф-ссылку клиентам!_"
        
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

        # Уникальные даты
        unique_dates = sorted(set(s.slot_date for s in slots))
        buttons = []
        row = []
        for d in unique_dates[:14]:
            row.append(InlineKeyboardButton(
                text=d.strftime("%d.%m"),
                callback_data=f"mgr_post_date:{d.isoformat()}"
            ))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_submit_post")])

        await callback.message.edit_text(
            f"📢 **{channel.name}**\n\n📅 Выберите дату размещения:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
        await state.set_state(ManagerPostStates.selecting_date)
    except Exception as e:
        logger.error(f"Error in mgr_submit_post_channel: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:100]}")


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

        if not slots:
            await callback.message.edit_text("😔 На эту дату нет доступных слотов.")
            return

        await state.update_data(mgr_selected_date=date_str)

        buttons = []
        for slot in slots:
            time_str = slot.slot_time.strftime("%H:%M")
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
            f"🕐 Выберите время:",
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
        if prices.get(fmt, 0) > 0:
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
    format_type = data.get("mgr_format_type", "1/24")
    price = data.get("mgr_price", 0)
    selected_date = data.get("mgr_selected_date", "")

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


@router.callback_query(F.data == "mgr_post_confirm", ManagerPostStates.confirming)
async def mgr_post_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Создание ScheduledPost со статусом moderation"""
    await callback.answer()

    data = await state.get_data()

    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            if not manager:
                await callback.message.answer("❌ Вы не менеджер")
                await state.clear()
                return

            slot_id = data.get("mgr_slot_id")
            slot = await session.get(Slot, slot_id)
            if not slot or slot.status != "available":
                await callback.message.edit_text(
                    "❌ Выбранный слот уже недоступен. Попробуйте снова.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="mgr_back")]
                    ])
                )
                await state.clear()
                return

            channel_id = data.get("mgr_channel_id")
            selected_date_str = data.get("mgr_selected_date", "")
            scheduled_time = datetime.combine(
                date.fromisoformat(selected_date_str),
                slot.slot_time
            )

            post = ScheduledPost(
                channel_id=channel_id,
                content=data.get("mgr_ad_content", ""),
                file_id=data.get("mgr_ad_file_id"),
                file_type=data.get("mgr_ad_file_type"),
                scheduled_time=scheduled_time,
                delete_after_hours=FORMAT_DELETE_HOURS.get(
                    data.get("mgr_format_type", "1/24"), 24
                ),
                status="moderation",
                created_by=callback.from_user.id
            )
            session.add(post)
            await session.commit()
            post_id = post.id

        await state.clear()

        await callback.message.edit_text(
            f"✅ **Пост #{post_id} отправлен на модерацию!**\n\n"
            f"Администратор рассмотрит его в ближайшее время.\n"
            f"После одобрения пост будет автоматически опубликован в указанное время.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ В кабинет", callback_data="mgr_back")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )

        # Уведомляем администраторов
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"📝 **Новый пост на модерацию #{post_id}**\n\n"
                    f"От менеджера: {callback.from_user.first_name or callback.from_user.username}\n"
                    f"📢 Канал ID: {channel_id}\n"
                    f"📅 Запланирован: {scheduled_time.strftime('%d.%m.%Y %H:%M')}\n\n"
                    f"Проверьте в разделе 📝 Модерация.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                logger.warning(f"Could not notify admin {admin_id} about new post #{post_id}", exc_info=True)

    except Exception as e:
        logger.error(f"Error in mgr_post_confirm: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка: {str(e)[:200]}")
        await state.clear()

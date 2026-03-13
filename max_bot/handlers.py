"""
Обработчики для бота в сети Max.

Реализует те же функции, что и Telegram-обработчики, но использует
библиотеку maxapi вместо aiogram. Пользователи Max хранятся в БД
по полю max_id (отдельному от telegram_id) во избежание коллизий ID.
"""
import logging
import traceback

from maxapi import Bot, Dispatcher, F
from maxapi.context import MemoryContext, State, StatesGroup
from maxapi.types import (
    BotStarted,
    Command,
    CommandStart,
    MessageCreated,
    MessageCallback,
)
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types import CallbackButton
from sqlalchemy import select

from config import ADMIN_IDS, ADMIN_PASSWORD, MANAGER_LEVELS, AVAILABLE_TIMEZONES
from database import async_session_maker, Manager, Client, Channel, Order
from max_bot.keyboards import (
    get_main_menu_markup,
    get_channels_markup,
    get_channel_detail_markup,
    get_admin_panel_markup,
    get_manager_cabinet_markup,
    get_training_markup,
    get_become_manager_markup,
    get_payout_markup,
    get_back_markup,
    get_admin_login_markup,
    get_confirm_markup,
)


logger = logging.getLogger(__name__)

# Множество авторизованных админов (по аналогии с Telegram-ботом)
authenticated_admins_max: set = set()


# ==================== FSM СОСТОЯНИЯ ====================

class AdminStates(StatesGroup):
    waiting_password = State()


class ManagerRegisterStates(StatesGroup):
    waiting_name = State()
    selecting_timezone = State()


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

async def _get_user_role(max_user_id: int):
    """Возвращает (manager, is_admin) по max_id пользователя."""
    is_admin = max_user_id in ADMIN_IDS
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.max_id == max_user_id)
        )
        manager = result.scalar_one_or_none()
    return manager, is_admin


# ==================== ПРИВЕТСТВИЕ ====================

async def _send_start(bot: Bot, chat_id: int, max_user_id: int, first_name: str):
    """Отправляет приветственное сообщение в зависимости от роли пользователя."""
    manager, is_admin = await _get_user_role(max_user_id)
    is_auth_admin = max_user_id in authenticated_admins_max

    if manager:
        level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
        text = (
            f"👋 С возвращением, {manager.first_name}!\n\n"
            f"{level_info['emoji']} Уровень: {level_info['name']}\n"
            f"💰 Баланс: {float(manager.balance):,.0f}₽\n"
            f"📦 Продаж: {manager.total_sales}"
        )
        markup = get_main_menu_markup(is_admin=is_admin, is_manager=True)
    elif is_admin:
        text = (
            "👋 Добро пожаловать, Администратор!\n\n"
            "Нажмите кнопку для входа в админ-панель."
        )
        markup = get_main_menu_markup(
            is_admin=True,
            is_authenticated_admin=is_auth_admin,
        )
    else:
        text = (
            "👋 Добро пожаловать!\n\n"
            "Здесь вы можете забронировать рекламу в наших каналах.\n\n"
            "📢 Выберите канал из каталога\n"
            "📅 Забронируйте дату и время\n"
            "💰 Оплатите размещение\n"
            "✅ Получите результат!"
        )
        markup = get_main_menu_markup(is_admin=False, is_manager=False)

    await bot.send_message(chat_id=chat_id, text=text, attachments=markup)


# ==================== НАСТРОЙКА ДИСПЕТЧЕРА ====================

def setup_max_dispatcher() -> Dispatcher:
    """Создаёт и настраивает диспетчер для Max-бота."""
    dp = Dispatcher()

    @dp.bot_started()
    async def on_bot_started(event: BotStarted):
        await _send_start(
            event.bot, event.chat_id,
            event.from_user.user_id,
            event.from_user.first_name or "",
        )

    @dp.message_created(CommandStart())
    async def cmd_start(event: MessageCreated, context: MemoryContext):
        await context.clear()
        user = event.message.sender
        await _send_start(
            event.bot,
            event.message.recipient.chat_id,
            user.user_id,
            user.first_name or "",
        )

    @dp.message_created(Command("help"))
    async def cmd_help(event: MessageCreated):
        text = (
            "📚 Справка\n\n"
            "Основные команды:\n"
            "/start — Главное меню\n"
            "/catalog — Каталог каналов\n"
            "/orders — Мои заказы\n\n"
            "Для менеджеров:\n"
            "/manager — Кабинет менеджера\n"
            "/training — Обучение\n\n"
            "Для админов:\n"
            "/admin — Админ-панель"
        )
        await event.message.answer(text)

    @dp.message_created(Command("catalog"))
    async def cmd_catalog(event: MessageCreated):
        await _show_catalog(event.bot, event.message.recipient.chat_id)

    async def _show_catalog(bot: Bot, chat_id: int):
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Channel).where(Channel.is_active == True)
                )
                channels = result.scalars().all()
                channels_data = [
                    {"id": ch.id, "name": ch.name, "prices": ch.prices or {}}
                    for ch in channels
                ]
            if not channels_data:
                await bot.send_message(chat_id=chat_id, text="😔 Каналов пока нет")
                return
            await bot.send_message(
                chat_id=chat_id,
                text="📢 Каталог каналов\n\nВыберите канал:",
                attachments=get_channels_markup(channels_data),
            )
        except Exception:
            logger.error(f"Ошибка _show_catalog: {traceback.format_exc()}")
            await bot.send_message(chat_id=chat_id, text="❌ Ошибка загрузки каталога")

    @dp.message_created(Command("orders"))
    async def cmd_orders(event: MessageCreated):
        max_user_id = event.message.sender.user_id
        try:
            async with async_session_maker() as session:
                client_res = await session.execute(
                    select(Client).where(Client.max_id == max_user_id)
                )
                client = client_res.scalar_one_or_none()
                if not client:
                    await event.message.answer("📦 У вас пока нет заказов")
                    return
                orders_res = await session.execute(
                    select(Order)
                    .where(Order.client_id == client.id)
                    .order_by(Order.created_at.desc())
                    .limit(10)
                )
                orders = orders_res.scalars().all()
            if not orders:
                await event.message.answer("📦 У вас пока нет заказов")
                return
            status_names = {
                "pending": "⏳ Ожидает оплаты",
                "payment_uploaded": "📤 Оплата на проверке",
                "payment_confirmed": "✅ Оплачен",
                "posted": "📝 Опубликован",
                "completed": "✔️ Завершён",
            }
            text = "📦 Ваши заказы:\n\n"
            for order in orders:
                status = status_names.get(order.status, "❓ " + order.status)
                text += f"• Заказ #{order.id} — {float(order.final_price):,.0f}₽ — {status}\n"
            await event.message.answer(text)
        except Exception:
            logger.error(f"Ошибка cmd_orders: {traceback.format_exc()}")
            await event.message.answer("❌ Ошибка загрузки заказов")

    @dp.message_created(Command("manager"))
    async def cmd_manager(event: MessageCreated):
        max_user_id = event.message.sender.user_id
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.max_id == max_user_id)
            )
            manager = result.scalar_one_or_none()
        if not manager:
            await event.message.answer(
                "❌ Вы не зарегистрированы как менеджер.\n\n"
                "Используйте кнопку «💼 Стать менеджером» в главном меню."
            )
            return
        level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
        text = (
            f"👤 Кабинет менеджера\n\n"
            f"{level_info['emoji']} {manager.first_name}\n"
            f"📊 Уровень: {level_info['name']}\n"
            f"💰 Баланс: {float(manager.balance):,.0f}₽\n"
            f"📦 Продаж: {manager.total_sales}\n"
            f"💵 Выручка: {float(manager.total_revenue):,.0f}₽"
        )
        await event.message.answer(text, attachments=get_manager_cabinet_markup())

    @dp.message_created(Command("training"))
    async def cmd_training(event: MessageCreated):
        max_user_id = event.message.sender.user_id
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.max_id == max_user_id)
            )
            manager = result.scalar_one_or_none()
        if not manager:
            await event.message.answer("❌ Обучение доступно только менеджерам")
            return
        await event.message.answer(
            "📚 Обучение менеджера\n\nВыберите раздел:",
            attachments=get_training_markup(),
        )

    @dp.message_created(Command("admin"))
    async def cmd_admin(event: MessageCreated):
        max_user_id = event.message.sender.user_id
        if max_user_id not in ADMIN_IDS:
            await event.message.answer("❌ У вас нет доступа к админ-панели")
            return
        if max_user_id in authenticated_admins_max:
            await event.message.answer(
                "⚙️ Админ-панель\n\nВыберите действие:",
                attachments=get_admin_panel_markup(),
            )
        else:
            await event.message.answer(
                "🔐 Вход в админ-панель\n\nНажмите кнопку для авторизации:",
                attachments=get_admin_login_markup(),
            )

    # ==================== CALLBACK-КНОПКИ ====================

    @dp.message_callback(F.callback.payload == "catalog")
    async def cb_catalog(event: MessageCallback):
        await event.answer(new_text="Загружаю каталог...")
        await _show_catalog(event.bot, event.message.recipient.chat_id)

    @dp.message_callback(F.callback.payload.startswith("channel:"))
    async def cb_channel(event: MessageCallback):
        try:
            channel_id = int(event.callback.payload.split(":")[1])
            async with async_session_maker() as session:
                channel = await session.get(Channel, channel_id)
                if not channel:
                    await event.answer(new_text="❌ Канал не найден")
                    return
            prices = channel.prices or {}
            text = (
                f"📢 {channel.name}\n\n"
                f"👥 Подписчиков: {channel.subscribers:,}\n"
                f"👁 Охват: {channel.avg_reach:,} просмотров/пост\n"
                f"📈 ERR: {channel.err_percent}%\n\n"
                f"💰 Стоимость размещения:\n"
                f"• 1/24: {prices.get('1/24', 0):,}₽\n"
                f"• 1/48: {prices.get('1/48', 0):,}₽\n"
                f"• 2/48: {prices.get('2/48', 0):,}₽\n"
                f"• Навсегда: {prices.get('native', 0):,}₽"
            )
            await event.answer(new_text=text)
            await event.bot.send_message(
                chat_id=event.message.recipient.chat_id,
                text=text,
                attachments=get_channel_detail_markup(channel_id),
            )
        except Exception:
            logger.error(f"Ошибка cb_channel: {traceback.format_exc()}")
            await event.answer(new_text="❌ Ошибка")

    @dp.message_callback(F.callback.payload == "my_orders")
    async def cb_my_orders(event: MessageCallback):
        max_user_id = event.callback.from_user.user_id
        try:
            async with async_session_maker() as session:
                client_res = await session.execute(
                    select(Client).where(Client.max_id == max_user_id)
                )
                client = client_res.scalar_one_or_none()
                if not client:
                    await event.answer(new_text="📦 У вас пока нет заказов")
                    return
                orders_res = await session.execute(
                    select(Order)
                    .where(Order.client_id == client.id)
                    .order_by(Order.created_at.desc())
                    .limit(10)
                )
                orders = orders_res.scalars().all()
            if not orders:
                await event.answer(new_text="📦 У вас пока нет заказов")
                return
            status_names = {
                "pending": "⏳ Ожидает оплаты",
                "payment_uploaded": "📤 Оплата на проверке",
                "payment_confirmed": "✅ Оплачен",
                "posted": "📝 Опубликован",
                "completed": "✔️ Завершён",
            }
            text = "📦 Ваши заказы:\n\n"
            for order in orders:
                status = status_names.get(order.status, "❓ " + order.status)
                text += f"• Заказ #{order.id} — {float(order.final_price):,.0f}₽ — {status}\n"
            await event.answer(new_text=text)
            await event.bot.send_message(
                chat_id=event.message.recipient.chat_id, text=text
            )
        except Exception:
            logger.error(f"Ошибка cb_my_orders: {traceback.format_exc()}")
            await event.answer(new_text="❌ Ошибка")

    @dp.message_callback(F.callback.payload == "become_manager")
    async def cb_become_manager(event: MessageCallback):
        max_user_id = event.callback.from_user.user_id
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.max_id == max_user_id)
            )
            existing = result.scalar_one_or_none()
        if existing:
            await event.answer(
                new_text="✅ Вы уже зарегистрированы как менеджер! Используйте /manager"
            )
            return
        await event.answer(new_text="Регистрация менеджера")
        await event.bot.send_message(
            chat_id=event.message.recipient.chat_id,
            text=(
                "💼 Стать менеджером\n\n"
                "Зарабатывайте на продаже рекламы!\n\n"
                "Условия:\n"
                "💰 Комиссия 10–25% от каждой продажи\n"
                "📚 Бесплатное обучение\n"
                "🏆 Бонусы за достижения\n"
                "📈 Карьерный рост"
            ),
            attachments=get_become_manager_markup(),
        )

    @dp.message_callback(F.callback.payload == "manager_register")
    async def cb_manager_register(event: MessageCallback, context: MemoryContext):
        max_user_id = event.callback.from_user.user_id
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.max_id == max_user_id)
            )
            existing = result.scalar_one_or_none()
        if existing:
            await event.answer(new_text="✅ Вы уже зарегистрированы!")
            return
        await context.set_state(ManagerRegisterStates.waiting_name)
        await event.answer(new_text="Как вас зовут?")
        await event.bot.send_message(
            chat_id=event.message.recipient.chat_id,
            text="✏️ Введите своё имя для регистрации:",
        )

    @dp.message_created(F.message.body.text, ManagerRegisterStates.waiting_name)
    async def process_manager_name(event: MessageCreated, context: MemoryContext):
        first_name = event.message.body.text.strip()
        if not first_name:
            await event.message.answer("❌ Имя не может быть пустым. Введите ещё раз:")
            return
        # Сохраняем имя и переходим к выбору timezone
        await context.set_data({"reg_first_name": first_name})
        await context.set_state(ManagerRegisterStates.selecting_timezone)
        builder = InlineKeyboardBuilder()
        for offset, label in AVAILABLE_TIMEZONES:
            builder.row(CallbackButton(text=label, payload=f"reg_tz:{offset}"))
        await event.message.answer(
            f"👋 {first_name}!\n\n"
            "🌍 Выберите ваш часовой пояс:\n"
            "(вы сможете изменить его позже в настройках)",
            attachments=builder.build(),
        )

    @dp.message_callback(F.callback.payload.startswith("reg_tz:"), ManagerRegisterStates.selecting_timezone)
    async def process_manager_timezone(event: MessageCallback, context: MemoryContext):
        tz_offset = int(event.callback.payload.split(":")[1])
        max_user_id = event.callback.from_user.user_id
        data = await context.get_data()
        first_name = data.get("reg_first_name", "Менеджер")
        try:
            sender = event.callback.from_user
            username = getattr(sender, "username", None) if sender else None
            async with async_session_maker() as session:
                new_manager = Manager(
                    max_id=max_user_id,
                    username=username,
                    first_name=first_name,
                    status="trainee",
                    level=1,
                    timezone_offset=tz_offset,
                )
                session.add(new_manager)
                await session.commit()
            await context.clear()
            tz_label = f"UTC{tz_offset:+d}"
            await event.answer(
                new_text=f"✅ Добро пожаловать, {first_name}!\n\n"
                f"🌍 Часовой пояс: {tz_label}\n"
                "Вы зарегистрированы как менеджер (Стажёр 🌱).\n"
                "Используйте /training для начала обучения.",
                attachments=get_main_menu_markup(is_manager=True),
            )
        except Exception:
            logger.error(f"Ошибка process_manager_timezone: {traceback.format_exc()}")
            await event.answer(new_text="❌ Ошибка регистрации. Попробуйте ещё раз.")

    @dp.message_callback(F.callback.payload == "profile")
    async def cb_profile(event: MessageCallback):
        max_user_id = event.callback.from_user.user_id
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.max_id == max_user_id)
            )
            manager = result.scalar_one_or_none()
        if not manager:
            await event.answer(new_text="❌ Вы не менеджер")
            return
        level_info = MANAGER_LEVELS.get(manager.level, MANAGER_LEVELS[1])
        text = (
            f"👤 Кабинет менеджера\n\n"
            f"{level_info['emoji']} {manager.first_name}\n"
            f"📊 Уровень: {level_info['name']}\n"
            f"💰 Баланс: {float(manager.balance):,.0f}₽\n"
            f"📦 Продаж: {manager.total_sales}\n"
            f"💵 Выручка: {float(manager.total_revenue):,.0f}₽"
        )
        await event.answer(new_text=text)
        await event.bot.send_message(
            chat_id=event.message.recipient.chat_id,
            text=text,
            attachments=get_manager_cabinet_markup(),
        )

    @dp.message_callback(F.callback.payload == "balance")
    async def cb_balance(event: MessageCallback):
        max_user_id = event.callback.from_user.user_id
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Manager).where(Manager.max_id == max_user_id)
                )
                manager = result.scalar_one_or_none()
            if not manager:
                await event.answer(new_text="❌ Вы не менеджер")
                return
            balance = float(manager.balance or 0)
            total_earned = float(manager.total_earned or 0)
            commission = MANAGER_LEVELS.get(manager.level, {}).get("commission", 10)
            text = (
                f"💰 Ваш баланс\n\n"
                f"💵 Доступно к выводу: {balance:,.0f}₽\n"
                f"📊 Всего заработано: {total_earned:,.0f}₽\n"
                f"📈 Ваша комиссия: {commission}%\n\n"
            )
            text += (
                "✅ Вы можете запросить вывод средств"
                if balance >= 500
                else "⚠️ Минимальная сумма для вывода: 500₽"
            )
            await event.answer(new_text=text)
            await event.bot.send_message(
                chat_id=event.message.recipient.chat_id,
                text=text,
                attachments=get_payout_markup(has_balance=balance >= 500),
            )
        except Exception:
            logger.error(f"Ошибка cb_balance: {traceback.format_exc()}")
            await event.answer(new_text="❌ Ошибка")

    @dp.message_callback(F.callback.payload == "training")
    async def cb_training(event: MessageCallback):
        max_user_id = event.callback.from_user.user_id
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.max_id == max_user_id)
            )
            manager = result.scalar_one_or_none()
        if not manager:
            await event.answer(new_text="❌ Обучение доступно только менеджерам")
            return
        await event.answer(new_text="Раздел обучения")
        await event.bot.send_message(
            chat_id=event.message.recipient.chat_id,
            text="📚 Обучение менеджера\n\nВыберите раздел:",
            attachments=get_training_markup(),
        )

    @dp.message_callback(F.callback.payload.in_({"leaderboard", "mgr_leaderboard"}))
    async def cb_leaderboard(event: MessageCallback):
        try:
            from services import gamification_service
            leaderboard = await gamification_service.get_leaderboard("sales", 10)
            if not leaderboard:
                await event.answer(new_text="📊 Рейтинг пока пуст")
                return
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            text = "🏆 Рейтинг менеджеров\n\n"
            for item in leaderboard:
                medal = medals.get(item["rank"], f"{item['rank']}.")
                text += f"{medal} {item['emoji']} {item['name']} — {item['sales']} продаж\n"
            await event.answer(new_text=text)
            await event.bot.send_message(
                chat_id=event.message.recipient.chat_id, text=text
            )
        except Exception:
            logger.error(f"Ошибка cb_leaderboard: {traceback.format_exc()}")
            await event.answer(new_text="❌ Ошибка")

    @dp.message_callback(F.callback.payload.in_({"templates", "mgr_templates"}))
    async def cb_templates(event: MessageCallback):
        max_user_id = event.callback.from_user.user_id
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.max_id == max_user_id)
            )
            manager = result.scalar_one_or_none()
        if not manager:
            await event.answer(new_text="❌ Вы не менеджер")
            return
        text = (
            "📋 Шаблоны для продаж\n\n"
            "🔥 Холодное сообщение:\n"
            "Привет! Хотите продвинуть свой канал/бизнес? "
            "У нас отличные цены на рекламу в топовых каналах! "
            "Напишите, расскажу подробнее.\n\n"
            "💰 Для рекламодателей:\n"
            "Добрый день! Предлагаю размещение в качественных каналах "
            "с живой аудиторией. Есть разные форматы и бюджеты. Какая у вас ниша?\n\n"
            "🎯 После интереса:\n"
            "Отлично! Вот ссылка для заказа: [ваша реф-ссылка]. "
            "Там можно выбрать канал, дату и формат. Если будут вопросы — пишите!"
        )
        await event.answer(new_text="Шаблоны")
        await event.bot.send_message(
            chat_id=event.message.recipient.chat_id, text=text
        )

    @dp.message_callback(F.callback.payload == "sales")
    async def cb_sales(event: MessageCallback):
        max_user_id = event.callback.from_user.user_id
        try:
            async with async_session_maker() as session:
                result = await session.execute(
                    select(Manager).where(Manager.max_id == max_user_id)
                )
                manager = result.scalar_one_or_none()
                if not manager:
                    await event.answer(new_text="❌ Вы не менеджер")
                    return
                result = await session.execute(
                    select(Channel).where(Channel.is_active == True)
                )
                channels = result.scalars().all()
                channels_data = [
                    {"id": ch.id, "name": ch.name, "prices": ch.prices or {}}
                    for ch in channels
                ]
            if not channels_data:
                await event.answer(new_text="😔 Каналов пока нет")
                return
            text = "💼 Каналы для продажи:\n\n"
            for ch in channels_data:
                price_124 = ch["prices"].get("1/24", 0)
                text += f"📢 {ch['name']} — от {price_124:,}₽\n"
            await event.answer(new_text="Каналы для продажи")
            await event.bot.send_message(
                chat_id=event.message.recipient.chat_id,
                text=text,
                attachments=get_channels_markup(channels_data),
            )
        except Exception:
            logger.error(f"Ошибка cb_sales: {traceback.format_exc()}")
            await event.answer(new_text="❌ Ошибка")

    @dp.message_callback(F.callback.payload.in_({"admin_login", "request_admin_password"}))
    async def cb_admin_login(event: MessageCallback, context: MemoryContext):
        max_user_id = event.callback.from_user.user_id
        if max_user_id not in ADMIN_IDS:
            await event.answer(new_text="❌ Нет доступа")
            return
        await context.set_state(AdminStates.waiting_password)
        await event.answer(new_text="Введите пароль администратора")
        await event.bot.send_message(
            chat_id=event.message.recipient.chat_id,
            text="🔐 Введите пароль администратора:",
        )

    @dp.message_created(F.message.body.text, AdminStates.waiting_password)
    async def process_admin_password(event: MessageCreated, context: MemoryContext):
        max_user_id = event.message.sender.user_id
        if max_user_id not in ADMIN_IDS:
            await context.clear()
            return
        entered = event.message.body.text.strip()
        if entered == ADMIN_PASSWORD:
            authenticated_admins_max.add(max_user_id)
            await context.clear()
            await event.message.answer(
                "✅ Авторизация успешна!\n\nДобро пожаловать в панель администратора.",
                attachments=get_admin_panel_markup(),
            )
        else:
            await event.message.answer("❌ Неверный пароль. Попробуйте ещё раз:")

    @dp.message_callback(F.callback.payload == "adm_logout")
    async def cb_adm_logout(event: MessageCallback):
        max_user_id = event.callback.from_user.user_id
        authenticated_admins_max.discard(max_user_id)
        await event.answer(new_text="🚪 Вы вышли из панели администратора")
        await _send_start(
            event.bot,
            event.message.recipient.chat_id,
            max_user_id,
            event.callback.from_user.first_name or "",
        )

    @dp.message_callback(F.callback.payload == "cancel")
    async def cb_cancel(event: MessageCallback):
        await event.answer(new_text="❌ Действие отменено")

    return dp

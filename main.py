"""
Telegram CRM Bot для продажи рекламы
Версия: 1.0 (single-file)
"""
import asyncio
import logging
import sys
from datetime import datetime, timedelta, time, date
from decimal import Decimal
from typing import Optional, List
from enum import Enum

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, DateTime, Date, Time,
    ForeignKey, Boolean, Numeric, JSON, Index, select, func, update
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.dialects.postgresql import JSONB

import os

# ==================== КОНФИГУРАЦИЯ ====================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8309573885:AAEOEdMajLBLDKxvqNrcckxpPkSVSFtQ2ek")
ADMIN_IDS = [942180996]
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/railway")

# Fix for Railway DATABASE_URL format
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

SLOT_TIMES = [time(9, 0), time(18, 0)]
RESERVATION_MINUTES = 15

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================

class Base(DeclarativeBase):
    pass

class Channel(Base):
    __tablename__ = "channels"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    username = Column(String(255))
    description = Column(Text)
    price_morning = Column(Numeric(12, 2), default=0)
    price_evening = Column(Numeric(12, 2), default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    slots = relationship("Slot", back_populates="channel", cascade="all, delete-orphan")

class Slot(Base):
    __tablename__ = "slots"
    
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    slot_date = Column(Date, nullable=False)
    slot_time = Column(Time, nullable=False)
    status = Column(String(20), default="available")  # available, reserved, booked
    reserved_until = Column(DateTime)
    reserved_by = Column(BigInteger)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    channel = relationship("Channel", back_populates="slots")
    order = relationship("Order", back_populates="slot", uselist=False)

class Client(Base):
    __tablename__ = "clients"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    total_orders = Column(Integer, default=0)
    total_spent = Column(Numeric(12, 2), default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    orders = relationship("Order", back_populates="client")

class Order(Base):
    __tablename__ = "orders"
    
    id = Column(Integer, primary_key=True)
    slot_id = Column(Integer, ForeignKey("slots.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    status = Column(String(30), default="awaiting_payment")
    ad_content = Column(Text)
    ad_format = Column(String(20))  # text, photo, video
    ad_file_id = Column(String(255))
    final_price = Column(Numeric(12, 2), nullable=False)
    payment_screenshot_file_id = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    
    slot = relationship("Slot", back_populates="order")
    client = relationship("Client", back_populates="orders")

# Engine and session
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")

# ==================== FSM СОСТОЯНИЯ ====================

class BookingStates(StatesGroup):
    selecting_channel = State()
    selecting_date = State()
    selecting_time = State()
    selecting_format = State()
    waiting_content = State()
    confirming = State()
    waiting_payment = State()
    uploading_screenshot = State()

class AdminChannelStates(StatesGroup):
    waiting_channel_forward = State()
    waiting_channel_name = State()
    waiting_price_morning = State()
    waiting_price_evening = State()

# ==================== ФИЛЬТРЫ ====================

class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in ADMIN_IDS

# ==================== КЛАВИАТУРЫ ====================

def get_main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📢 Каталог каналов")],
        [KeyboardButton(text="📦 Мои заказы")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_admin_menu() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="📢 Каналы"), KeyboardButton(text="💳 Оплаты")],
        [KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="◀️ Главное меню")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_channels_keyboard(channels: List[Channel]) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        price = min(ch.price_morning or 0, ch.price_evening or 0)
        buttons.append([InlineKeyboardButton(
            text=f"{ch.name} — от {price:,.0f}₽",
            callback_data=f"channel:{ch.id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_dates_keyboard(slots: List[Slot]) -> InlineKeyboardMarkup:
    dates = sorted(set(s.slot_date for s in slots))[:14]
    buttons = []
    for d in dates:
        buttons.append([InlineKeyboardButton(
            text=d.strftime("%d.%m.%Y (%a)"),
            callback_data=f"date:{d.isoformat()}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_channels")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_slots_keyboard(slots: List[Slot], channel: Channel) -> InlineKeyboardMarkup:
    buttons = []
    for slot in slots:
        if slot.slot_time.hour < 12:
            price = channel.price_morning
            emoji = "🌅"
        else:
            price = channel.price_evening
            emoji = "🌆"
        buttons.append([InlineKeyboardButton(
            text=f"{emoji} {slot.slot_time.strftime('%H:%M')} — {price:,.0f}₽",
            callback_data=f"slot:{slot.id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_dates")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Текст", callback_data="format:text")],
        [InlineKeyboardButton(text="🖼 Фото + текст", callback_data="format:photo")],
        [InlineKeyboardButton(text="🎬 Видео + текст", callback_data="format:video")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])

def get_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_order")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])

def get_payment_review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"approve:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{order_id}"),
        ]
    ])

def get_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

# ==================== РОУТЕРЫ ====================

router = Router()

# --- Команда /start ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    is_admin = message.from_user.id in ADMIN_IDS
    role = "администратор" if is_admin else "клиент"
    
    await message.answer(
        f"👋 Добро пожаловать в CRM-бот!\n\n"
        f"Здесь вы можете забронировать рекламу в наших каналах.\n\n"
        f"🔑 Ваша роль: **{role}**",
        reply_markup=get_main_menu(is_admin),
        parse_mode=ParseMode.MARKDOWN
    )

# --- Каталог каналов ---
@router.message(F.text == "📢 Каталог каналов")
async def show_catalog(message: Message, state: FSMContext):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Channel).where(Channel.is_active == True)
        )
        channels = result.scalars().all()
    
    if not channels:
        await message.answer("😔 Пока нет доступных каналов")
        return
    
    await message.answer(
        "📢 **Выберите канал для размещения:**",
        reply_markup=get_channels_keyboard(channels),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.selecting_channel)

# --- Выбор канала ---
@router.callback_query(F.data.startswith("channel:"))
async def select_channel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    channel_id = int(callback.data.split(":")[1])
    
    async with async_session_maker() as session:
        channel = await session.get(Channel, channel_id)
        result = await session.execute(
            select(Slot).where(
                Slot.channel_id == channel_id,
                Slot.status == "available",
                Slot.slot_date >= date.today()
            ).order_by(Slot.slot_date)
        )
        slots = result.scalars().all()
    
    if not slots:
        await callback.message.edit_text("😔 Нет доступных слотов")
        return
    
    await state.update_data(channel_id=channel_id, channel_name=channel.name)
    
    await callback.message.edit_text(
        f"📢 **{channel.name}**\n\n"
        f"🌅 Утро (9:00): {channel.price_morning:,.0f}₽\n"
        f"🌆 Вечер (18:00): {channel.price_evening:,.0f}₽\n\n"
        f"Выберите дату:",
        reply_markup=get_dates_keyboard(slots),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.selecting_date)

# --- Выбор даты ---
@router.callback_query(F.data.startswith("date:"), BookingStates.selecting_date)
async def select_date(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    date_str = callback.data.split(":")[1]
    selected_date = date.fromisoformat(date_str)
    
    data = await state.get_data()
    channel_id = data["channel_id"]
    
    async with async_session_maker() as session:
        channel = await session.get(Channel, channel_id)
        result = await session.execute(
            select(Slot).where(
                Slot.channel_id == channel_id,
                Slot.slot_date == selected_date,
                Slot.status == "available"
            ).order_by(Slot.slot_time)
        )
        slots = result.scalars().all()
    
    if not slots:
        await callback.message.edit_text("😔 На эту дату нет слотов")
        return
    
    await state.update_data(selected_date=date_str)
    
    await callback.message.edit_text(
        f"📅 **{selected_date.strftime('%d.%m.%Y')}**\n\n"
        f"Выберите время:",
        reply_markup=get_slots_keyboard(slots, channel),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.selecting_time)

# --- Выбор слота ---
@router.callback_query(F.data.startswith("slot:"), BookingStates.selecting_time)
async def select_slot(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    slot_id = int(callback.data.split(":")[1])
    
    async with async_session_maker() as session:
        slot = await session.get(Slot, slot_id)
        
        if not slot or slot.status != "available":
            await callback.message.edit_text("😔 Этот слот уже занят")
            return
        
        # Резервируем слот
        slot.status = "reserved"
        slot.reserved_by = callback.from_user.id
        slot.reserved_until = datetime.utcnow() + timedelta(minutes=RESERVATION_MINUTES)
        await session.commit()
        
        channel = await session.get(Channel, slot.channel_id)
        price = channel.price_morning if slot.slot_time.hour < 12 else channel.price_evening
    
    await state.update_data(slot_id=slot_id, price=float(price))
    
    await callback.message.edit_text(
        f"✅ Слот зарезервирован на {RESERVATION_MINUTES} минут!\n\n"
        f"Выберите формат рекламы:",
        reply_markup=get_format_keyboard()
    )
    await state.set_state(BookingStates.selecting_format)

# --- Выбор формата ---
@router.callback_query(F.data.startswith("format:"), BookingStates.selecting_format)
async def select_format(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    ad_format = callback.data.split(":")[1]
    await state.update_data(ad_format=ad_format)
    
    format_hints = {
        "text": "📝 Отправьте текст рекламного поста:",
        "photo": "🖼 Отправьте фото с подписью:",
        "video": "🎬 Отправьте видео с подписью:"
    }
    
    await callback.message.edit_text(
        format_hints[ad_format],
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(BookingStates.waiting_content)

# --- Получение контента ---
@router.message(BookingStates.waiting_content)
async def receive_content(message: Message, state: FSMContext):
    data = await state.get_data()
    ad_format = data["ad_format"]
    
    content = None
    file_id = None
    
    if ad_format == "text" and message.text:
        content = message.text
    elif ad_format == "photo" and message.photo:
        content = message.caption or ""
        file_id = message.photo[-1].file_id
    elif ad_format == "video" and message.video:
        content = message.caption or ""
        file_id = message.video.file_id
    else:
        await message.answer(f"❌ Отправьте {'текст' if ad_format == 'text' else 'фото' if ad_format == 'photo' else 'видео'}")
        return
    
    await state.update_data(ad_content=content, ad_file_id=file_id)
    
    price = data["price"]
    channel_name = data["channel_name"]
    selected_date = data["selected_date"]
    
    await message.answer(
        f"📋 **Подтверждение заказа**\n\n"
        f"📢 Канал: {channel_name}\n"
        f"📅 Дата: {selected_date}\n"
        f"📝 Формат: {ad_format}\n"
        f"💰 Цена: **{price:,.0f}₽**\n\n"
        f"Подтвердите заказ:",
        reply_markup=get_confirm_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.confirming)

# --- Подтверждение заказа ---
@router.callback_query(F.data == "confirm_order", BookingStates.confirming)
async def confirm_order(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    
    async with async_session_maker() as session:
        # Получаем или создаём клиента
        result = await session.execute(
            select(Client).where(Client.telegram_id == callback.from_user.id)
        )
        client = result.scalar_one_or_none()
        
        if not client:
            client = Client(
                telegram_id=callback.from_user.id,
                username=callback.from_user.username,
                first_name=callback.from_user.first_name
            )
            session.add(client)
            await session.flush()
        
        # Обновляем слот
        slot = await session.get(Slot, data["slot_id"])
        slot.status = "booked"
        
        # Создаём заказ
        order = Order(
            slot_id=data["slot_id"],
            client_id=client.id,
            ad_content=data.get("ad_content"),
            ad_format=data["ad_format"],
            ad_file_id=data.get("ad_file_id"),
            final_price=Decimal(str(data["price"]))
        )
        session.add(order)
        await session.commit()
        
        order_id = order.id
    
    await state.update_data(order_id=order_id)
    
    await callback.message.edit_text(
        f"✅ **Заказ #{order_id} создан!**\n\n"
        f"💳 Для оплаты переведите **{data['price']:,.0f}₽** на карту:\n\n"
        f"`4276 1234 5678 9012`\n\n"
        f"После оплаты отправьте скриншот чека:",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.uploading_screenshot)

# --- Получение скриншота оплаты ---
@router.message(BookingStates.uploading_screenshot, F.photo)
async def receive_payment_screenshot(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    order_id = data["order_id"]
    file_id = message.photo[-1].file_id
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(Order).where(Order.id == order_id)
        )
        order = result.scalar_one_or_none()
        
        if order:
            order.payment_screenshot_file_id = file_id
            order.status = "payment_uploaded"
            await session.commit()
    
    await message.answer(
        f"✅ Скриншот получен!\n\n"
        f"Ожидайте подтверждения оплаты. Обычно это занимает до 30 минут.",
        reply_markup=get_main_menu(message.from_user.id in ADMIN_IDS)
    )
    await state.clear()
    
    # Уведомляем админов
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_photo(
                admin_id,
                photo=file_id,
                caption=f"💳 **Новая оплата!**\n\n"
                        f"Заказ: #{order_id}\n"
                        f"От: {message.from_user.first_name}\n\n"
                        f"Проверьте оплату:",
                reply_markup=get_payment_review_keyboard(order_id),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")

# --- Мои заказы ---
@router.message(F.text == "📦 Мои заказы")
async def show_my_orders(message: Message):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Order)
            .join(Client)
            .where(Client.telegram_id == message.from_user.id)
            .order_by(Order.created_at.desc())
            .limit(10)
        )
        orders = result.scalars().all()
    
    if not orders:
        await message.answer("📦 У вас пока нет заказов")
        return
    
    text = "📦 **Ваши заказы:**\n\n"
    status_emoji = {
        "awaiting_payment": "⏳",
        "payment_uploaded": "🔄",
        "payment_confirmed": "✅",
        "completed": "✅",
        "cancelled": "❌"
    }
    
    for order in orders:
        emoji = status_emoji.get(order.status, "❓")
        text += f"{emoji} Заказ #{order.id} — {order.final_price:,.0f}₽\n"
    
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# --- Отмена ---
@router.callback_query(F.data == "cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Отменено")
    
    # Освобождаем слот если был зарезервирован
    data = await state.get_data()
    if "slot_id" in data:
        async with async_session_maker() as session:
            slot = await session.get(Slot, data["slot_id"])
            if slot and slot.status == "reserved":
                slot.status = "available"
                slot.reserved_by = None
                slot.reserved_until = None
                await session.commit()
    
    await state.clear()
    await callback.message.edit_text("❌ Действие отменено")

@router.callback_query(F.data == "back_to_channels")
async def back_to_channels(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(BookingStates.selecting_channel)
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(Channel).where(Channel.is_active == True)
        )
        channels = result.scalars().all()
    
    await callback.message.edit_text(
        "📢 **Выберите канал:**",
        reply_markup=get_channels_keyboard(channels),
        parse_mode=ParseMode.MARKDOWN
    )

# ==================== АДМИН-ПАНЕЛЬ ====================

@router.message(F.text == "⚙️ Админ-панель", IsAdmin())
async def admin_panel(message: Message):
    await message.answer(
        "⚙️ **Админ-панель**",
        reply_markup=get_admin_menu(),
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(F.text == "◀️ Главное меню")
async def back_to_main(message: Message):
    is_admin = message.from_user.id in ADMIN_IDS
    await message.answer("🏠 Главное меню", reply_markup=get_main_menu(is_admin))

# --- Список каналов (админ) ---
@router.message(F.text == "📢 Каналы", IsAdmin())
async def admin_channels(message: Message, state: FSMContext):
    async with async_session_maker() as session:
        result = await session.execute(select(Channel))
        channels = result.scalars().all()
    
    if channels:
        text = "📢 **Каналы:**\n\n"
        for ch in channels:
            status = "✅" if ch.is_active else "❌"
            text += f"{status} {ch.name}\n   🌅 {ch.price_morning:,.0f}₽ | 🌆 {ch.price_evening:,.0f}₽\n\n"
    else:
        text = "📢 Каналов пока нет\n\n"
    
    text += "Чтобы добавить канал, отправьте /add\\_channel"
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# --- Добавление канала ---
@router.message(Command("add_channel"), IsAdmin())
async def start_add_channel(message: Message, state: FSMContext):
    await message.answer(
        "📢 **Добавление канала**\n\n"
        "Перешлите любое сообщение из канала:",
        reply_markup=get_cancel_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminChannelStates.waiting_channel_forward)

@router.message(AdminChannelStates.waiting_channel_forward)
async def receive_channel_forward(message: Message, state: FSMContext):
    if not message.forward_from_chat:
        await message.answer("❌ Перешлите сообщение из канала")
        return
    
    chat = message.forward_from_chat
    await state.update_data(
        telegram_id=chat.id,
        username=chat.username,
        name=chat.title
    )
    
    await message.answer(
        f"✅ Канал: **{chat.title}**\n\n"
        f"Введите цену за утренний слот (9:00) в рублях:",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminChannelStates.waiting_price_morning)

@router.message(AdminChannelStates.waiting_price_morning)
async def receive_price_morning(message: Message, state: FSMContext):
    try:
        price = Decimal(message.text.strip())
    except:
        await message.answer("❌ Введите число")
        return
    
    await state.update_data(price_morning=str(price))
    await message.answer("Введите цену за вечерний слот (18:00):")
    await state.set_state(AdminChannelStates.waiting_price_evening)

@router.message(AdminChannelStates.waiting_price_evening)
async def receive_price_evening(message: Message, state: FSMContext):
    try:
        price = Decimal(message.text.strip())
    except:
        await message.answer("❌ Введите число")
        return
    
    data = await state.get_data()
    
    async with async_session_maker() as session:
        channel = Channel(
            telegram_id=data["telegram_id"],
            name=data["name"],
            username=data.get("username"),
            price_morning=Decimal(data["price_morning"]),
            price_evening=price
        )
        session.add(channel)
        await session.flush()
        
        # Создаём слоты на 30 дней
        today = date.today()
        for i in range(30):
            slot_date = today + timedelta(days=i)
            for slot_time in SLOT_TIMES:
                slot = Slot(
                    channel_id=channel.id,
                    slot_date=slot_date,
                    slot_time=slot_time
                )
                session.add(slot)
        
        await session.commit()
    
    await message.answer(
        f"✅ **Канал добавлен!**\n\n"
        f"📢 {data['name']}\n"
        f"🌅 Утро: {data['price_morning']}₽\n"
        f"🌆 Вечер: {price}₽\n"
        f"📅 Создано 60 слотов",
        reply_markup=get_admin_menu(),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.clear()

# --- Проверка оплат ---
@router.message(F.text == "💳 Оплаты", IsAdmin())
async def admin_payments(message: Message):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Order)
            .where(Order.status == "payment_uploaded")
            .order_by(Order.created_at.desc())
        )
        orders = result.scalars().all()
    
    if not orders:
        await message.answer("✅ Нет оплат на проверке")
        return
    
    await message.answer(f"💳 Оплат на проверке: {len(orders)}\n\nИспользуйте /check ID")

@router.message(Command("check"), IsAdmin())
async def check_payment(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /check ID")
        return
    
    try:
        order_id = int(args[1])
    except:
        await message.answer("❌ Неверный ID")
        return
    
    async with async_session_maker() as session:
        order = await session.get(Order, order_id)
        
        if not order or not order.payment_screenshot_file_id:
            await message.answer("❌ Заказ не найден")
            return
    
    await message.answer_photo(
        photo=order.payment_screenshot_file_id,
        caption=f"💳 Заказ #{order.id}\n💰 {order.final_price:,.0f}₽",
        reply_markup=get_payment_review_keyboard(order.id)
    )

# --- Подтверждение/отклонение оплаты ---
@router.callback_query(F.data.startswith("approve:"), IsAdmin())
async def approve_payment(callback: CallbackQuery, bot: Bot):
    await callback.answer("✅ Подтверждено")
    order_id = int(callback.data.split(":")[1])
    
    async with async_session_maker() as session:
        order = await session.get(Order, order_id)
        if order:
            order.status = "payment_confirmed"
            
            # Обновляем клиента
            result = await session.execute(
                select(Client).where(Client.id == order.client_id)
            )
            client = result.scalar_one_or_none()
            if client:
                client.total_orders += 1
                client.total_spent += order.final_price
                
                # Уведомляем клиента
                try:
                    await bot.send_message(
                        client.telegram_id,
                        f"✅ **Оплата подтверждена!**\n\n"
                        f"Заказ #{order_id} принят в работу.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
            
            await session.commit()
    
    await callback.message.edit_caption(
        callback.message.caption + "\n\n✅ ПОДТВЕРЖДЕНО"
    )

@router.callback_query(F.data.startswith("reject:"), IsAdmin())
async def reject_payment(callback: CallbackQuery, bot: Bot):
    await callback.answer("❌ Отклонено")
    order_id = int(callback.data.split(":")[1])
    
    async with async_session_maker() as session:
        order = await session.get(Order, order_id)
        if order:
            order.status = "cancelled"
            
            # Освобождаем слот
            slot = await session.get(Slot, order.slot_id)
            if slot:
                slot.status = "available"
            
            result = await session.execute(
                select(Client).where(Client.id == order.client_id)
            )
            client = result.scalar_one_or_none()
            if client:
                try:
                    await bot.send_message(
                        client.telegram_id,
                        f"❌ **Оплата не подтверждена**\n\n"
                        f"Заказ #{order_id} отклонён. Свяжитесь с поддержкой.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
            
            await session.commit()
    
    await callback.message.edit_caption(
        callback.message.caption + "\n\n❌ ОТКЛОНЕНО"
    )

# --- Статистика ---
@router.message(F.text == "📊 Статистика", IsAdmin())
async def admin_stats(message: Message):
    async with async_session_maker() as session:
        # Всего заказов
        orders_count = await session.execute(select(func.count(Order.id)))
        total_orders = orders_count.scalar() or 0
        
        # Выручка
        revenue = await session.execute(
            select(func.sum(Order.final_price))
            .where(Order.status == "payment_confirmed")
        )
        total_revenue = revenue.scalar() or 0
        
        # Каналов
        channels_count = await session.execute(
            select(func.count(Channel.id)).where(Channel.is_active == True)
        )
        total_channels = channels_count.scalar() or 0
    
    await message.answer(
        f"📊 **Статистика**\n\n"
        f"📦 Всего заказов: {total_orders}\n"
        f"💰 Выручка: {total_revenue:,.0f}₽\n"
        f"📢 Каналов: {total_channels}",
        parse_mode=ParseMode.MARKDOWN
    )

# ==================== ЗАПУСК ====================

async def on_startup(bot: Bot):
    await init_db()
    me = await bot.get_me()
    logger.info(f"Bot started: @{me.username}")
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🤖 Бот запущен!\n\n@{me.username}")
        except:
            pass

async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.startup.register(on_startup)
    
    logger.info("Starting bot...")
    await dp.start_polling(bot, drop_pending_updates=True)

if __name__ == "__main__":
    asyncio.run(main())

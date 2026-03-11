"""
Обработчики для клиентов (бронирование, заказы)
"""
import logging
import traceback
from datetime import date, datetime, timedelta
from decimal import Decimal

from aiogram import Router, Bot, F
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from config import CHANNEL_CATEGORIES, ADMIN_IDS
from database import async_session_maker, Channel, Slot, Client, Order, Manager
from keyboards import get_channels_keyboard, get_dates_keyboard, get_times_keyboard, get_format_keyboard
from utils import BookingStates


logger = logging.getLogger(__name__)
router = Router()


# ==================== КАТАЛОГ КАНАЛОВ ====================

@router.callback_query(F.data == "back_to_channels")
async def back_to_channels(callback: CallbackQuery, state: FSMContext):
    """Назад к списку каналов"""
    await callback.answer()
    await state.clear()
    
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
            await callback.message.edit_text("😔 Каналов пока нет")
            return
        
        await callback.message.edit_text(
            "📢 **Каталог каналов**\n\nВыберите канал:",
            reply_markup=get_channels_keyboard(channels_data),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in back_to_channels: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== ВЫБОР КАНАЛА ====================

@router.callback_query(F.data.startswith("channel:"))
async def select_channel(callback: CallbackQuery, state: FSMContext):
    """Выбор канала для бронирования"""
    await callback.answer()
    
    try:
        channel_id = int(callback.data.split(":")[1])
        
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            
            if not channel:
                await callback.message.edit_text("❌ Канал не найден")
                return
            
            # Получаем слоты
            result = await session.execute(
                select(Slot).where(
                    Slot.channel_id == channel_id,
                    Slot.status == "available",
                    Slot.slot_date >= date.today()
                ).order_by(Slot.slot_date)
            )
            slots = result.scalars().all()
            
            # Сохраняем данные канала
            ch_data = {
                "name": channel.name,
                "category": channel.category,
                "subscribers": channel.subscribers or 0,
                "avg_reach": channel.avg_reach_24h or channel.avg_reach or 0,
                "prices": channel.prices or {}
            }
        
        category_info = CHANNEL_CATEGORIES.get(ch_data["category"], {"name": "📁 Другое"})
        prices = ch_data["prices"]
        
        text = (
            f"📢 **{ch_data['name']}**\n"
            f"{category_info['name']}\n\n"
            f"👥 Подписчиков: **{ch_data['subscribers']:,}**\n"
            f"👁 Охват: **{ch_data['avg_reach']:,}**\n\n"
            f"💰 **Цены:**\n"
            f"• 1/24: {prices.get('1/24', 0):,}₽\n"
            f"• 1/48: {prices.get('1/48', 0):,}₽\n"
            f"• 2/48: {prices.get('2/48', 0):,}₽\n"
            f"• Навсегда: {prices.get('native', 0):,}₽\n\n"
        )
        
        if not slots:
            text += "😔 _Нет доступных слотов_"
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_channels")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        text += "📅 Выберите дату:"
        
        await state.update_data(channel_id=channel_id, channel_name=ch_data["name"], prices=prices)
        
        await callback.message.edit_text(
            text,
            reply_markup=get_dates_keyboard(slots),
            parse_mode=ParseMode.MARKDOWN
        )
        await state.set_state(BookingStates.selecting_date)
    except Exception as e:
        logger.error(f"Error in select_channel: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== ВЫБОР ДАТЫ ====================

@router.callback_query(F.data == "back_to_dates")
async def back_to_dates(callback: CallbackQuery, state: FSMContext):
    """Назад к выбору даты"""
    await callback.answer()
    
    data = await state.get_data()
    channel_id = data.get("channel_id")
    
    if not channel_id:
        await callback.message.edit_text("❌ Ошибка. Начните заново.")
        await state.clear()
        return
    
    # Перенаправляем на выбор канала
    callback.data = f"channel:{channel_id}"
    await select_channel(callback, state)


@router.callback_query(F.data.startswith("date:"), BookingStates.selecting_date)
async def select_date(callback: CallbackQuery, state: FSMContext):
    """Выбор даты"""
    await callback.answer()
    
    date_str = callback.data.split(":")[1]
    selected_date = date.fromisoformat(date_str)
    
    data = await state.get_data()
    channel_id = data.get("channel_id")
    prices = data.get("prices", {})
    
    try:
        async with async_session_maker() as session:
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
            f"📅 **{selected_date.strftime('%d.%m.%Y')}**\n\nВыберите время:",
            reply_markup=get_times_keyboard(slots, prices),
            parse_mode=ParseMode.MARKDOWN
        )
        await state.set_state(BookingStates.selecting_time)
    except Exception as e:
        logger.error(f"Error in select_date: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== ВЫБОР ВРЕМЕНИ ====================

@router.callback_query(F.data == "back_to_times")
async def back_to_times(callback: CallbackQuery, state: FSMContext):
    """Назад к выбору времени"""
    await callback.answer()
    
    data = await state.get_data()
    date_str = data.get("selected_date")
    
    if date_str:
        callback.data = f"date:{date_str}"
        await state.set_state(BookingStates.selecting_date)
        await select_date(callback, state)
    else:
        await callback.message.edit_text("❌ Ошибка. Начните заново.")
        await state.clear()


@router.callback_query(F.data.startswith("time:"), BookingStates.selecting_time)
async def select_time(callback: CallbackQuery, state: FSMContext):
    """Выбор времени"""
    await callback.answer()
    
    slot_id = int(callback.data.split(":")[1])
    
    data = await state.get_data()
    channel_id = data.get("channel_id")
    
    await state.update_data(slot_id=slot_id)
    
    await callback.message.edit_text(
        "📋 **Выберите формат размещения:**",
        reply_markup=get_format_keyboard(channel_id),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.selecting_format)


# ==================== ВЫБОР ФОРМАТА ====================

@router.callback_query(F.data.startswith("format:"), BookingStates.selecting_format)
async def select_format(callback: CallbackQuery, state: FSMContext):
    """Выбор формата размещения"""
    await callback.answer()
    
    format_type = callback.data.split(":")[1]
    
    data = await state.get_data()
    prices = data.get("prices", {})
    channel_name = data.get("channel_name", "Канал")
    
    price = prices.get(format_type, 0)
    
    await state.update_data(format_type=format_type, price=price)
    
    format_names = {
        "1/24": "1/24 (24 часа)",
        "1/48": "1/48 (48 часов)",
        "2/48": "2/48 (2 поста)",
        "native": "Навсегда"
    }
    
    await callback.message.edit_text(
        f"📝 **Отправьте рекламный материал**\n\n"
        f"📢 Канал: {channel_name}\n"
        f"📋 Формат: {format_names.get(format_type, format_type)}\n"
        f"💰 Цена: **{price:,}₽**\n\n"
        f"Отправьте текст поста (можно с фото/видео):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.entering_content)


# ==================== КОНТЕНТ ====================

@router.message(BookingStates.entering_content)
async def receive_content(message: Message, state: FSMContext):
    """Получение рекламного контента"""
    
    content_text = message.text or message.caption or ""
    file_id = None
    file_type = None
    
    # Определяем тип медиа
    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    
    await state.update_data(
        ad_content=content_text,
        ad_file_id=file_id,
        ad_file_type=file_type
    )
    
    data = await state.get_data()
    channel_name = data.get("channel_name", "Канал")
    format_type = data.get("format_type", "1/24")
    price = data.get("price", 0)
    selected_date = data.get("selected_date", "")
    
    # Показываем превью
    text = (
        f"✅ **Подтверждение заказа**\n\n"
        f"📢 Канал: {channel_name}\n"
        f"📅 Дата: {selected_date}\n"
        f"📋 Формат: {format_type}\n"
        f"💰 Цена: **{price:,}₽**\n\n"
    )
    
    if content_text:
        text += f"📝 Текст:\n{content_text[:200]}{'...' if len(content_text) > 200 else ''}\n\n"
    
    if file_id:
        text += f"📎 Медиа: {file_type}\n\n"
    
    text += "Всё верно?"
    
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_order"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
            ]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.confirming)


# ==================== ПОДТВЕРЖДЕНИЕ ====================

@router.callback_query(F.data == "confirm_order", BookingStates.confirming)
async def confirm_order(callback: CallbackQuery, state: FSMContext):
    """Подтверждение заказа"""
    await callback.answer()
    
    data = await state.get_data()
    user = callback.from_user
    
    try:
        async with async_session_maker() as session:
            # Получаем или создаём клиента
            result = await session.execute(
                select(Client).where(Client.telegram_id == user.id)
            )
            client = result.scalar_one_or_none()
            
            if not client:
                client = Client(
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name
                )
                session.add(client)
                await session.flush()
            
            # Бронируем слот
            slot = await session.get(Slot, data.get("slot_id"))
            if not slot or slot.status != "available":
                await callback.message.edit_text("❌ Слот уже занят. Выберите другой.")
                await state.clear()
                return
            
            slot.status = "reserved"
            slot.reserved_by = user.id
            slot.reserved_until = datetime.utcnow() + timedelta(hours=24)
            
            # Определяем менеджера по реферальной ссылке клиента
            manager_id = None
            if client.referrer_id:
                manager_id = client.referrer_id
            
            # Создаём заказ
            order = Order(
                slot_id=slot.id,
                client_id=client.id,
                manager_id=manager_id,
                format_type=data.get("format_type", "1/24"),
                base_price=Decimal(str(data.get("price", 0))),
                final_price=Decimal(str(data.get("price", 0))),
                ad_content=data.get("ad_content"),
                ad_file_id=data.get("ad_file_id"),
                ad_file_type=data.get("ad_file_type"),
                status="pending"
            )
            session.add(order)
            await session.commit()
            
            order_id = order.id
            price = float(order.final_price)
        
        await state.clear()
        
        await callback.message.edit_text(
            f"✅ **Заказ #{order_id} создан!**\n\n"
            f"💰 К оплате: **{price:,.0f}₽**\n\n"
            f"📤 Отправьте скриншот оплаты для подтверждения.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Загрузить скриншот", callback_data=f"upload_payment:{order_id}")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in confirm_order: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)
        await state.clear()


# ==================== ОТМЕНА ====================

@router.callback_query(F.data == "cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext):
    """Отмена действия"""
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("❌ Действие отменено")


# ==================== ЗАГРУЗКА ОПЛАТЫ ====================

@router.callback_query(F.data.startswith("upload_payment:"))
async def upload_payment_start(callback: CallbackQuery, state: FSMContext):
    """Начать загрузку скриншота оплаты"""
    await callback.answer()
    
    order_id = int(callback.data.split(":")[1])
    await state.update_data(payment_order_id=order_id)
    
    await callback.message.edit_text(
        "📤 **Загрузите скриншот оплаты**\n\n"
        "Отправьте фото или документ:",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.uploading_payment)


@router.message(BookingStates.uploading_payment)
async def receive_payment_screenshot(message: Message, state: FSMContext, bot: Bot):
    """Получение скриншота оплаты"""
    
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
    
    if not file_id:
        await message.answer("❌ Отправьте фото или документ")
        return
    
    data = await state.get_data()
    order_id = data.get("payment_order_id")
    
    try:
        async with async_session_maker() as session:
            order = await session.get(Order, order_id)
            
            if not order:
                await message.answer("❌ Заказ не найден")
                await state.clear()
                return
            
            order.payment_screenshot = file_id
            order.status = "payment_uploaded"
            await session.commit()
        
        await state.clear()
        
        await message.answer(
            f"✅ **Скриншот загружен!**\n\n"
            f"Заказ #{order_id} отправлен на проверку.\n"
            f"Ожидайте подтверждения от администратора.",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Уведомляем админов
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"💳 **Новая оплата!**\n\nЗаказ #{order_id}\nПроверьте в админ-панели.",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass
    except Exception as e:
        logger.error(f"Error in receive_payment_screenshot: {traceback.format_exc()}")
        await message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)
        await state.clear()

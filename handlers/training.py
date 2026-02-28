"""
Обработчики обучения менеджеров
"""
import logging
import traceback

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select

from config import DEFAULT_LESSONS
from database import async_session_maker, Manager
from keyboards import get_training_menu, get_ai_feedback_keyboard
from utils import ManagerStates
from services import ai_trainer_service


logger = logging.getLogger(__name__)
router = Router()


# ==================== МЕНЮ ОБУЧЕНИЯ ====================

@router.callback_query(F.data == "back_to_training")
async def back_to_training(callback: CallbackQuery, state: FSMContext):
    """Назад к меню обучения"""
    await callback.answer()
    await state.clear()
    
    await callback.message.edit_text(
        "📚 **Обучение менеджера**\n\nВыберите раздел:",
        reply_markup=get_training_menu(),
        parse_mode=ParseMode.MARKDOWN
    )


# ==================== УРОКИ ====================

@router.callback_query(F.data == "show_lessons")
async def show_lessons(callback: CallbackQuery, state: FSMContext):
    """Показать список уроков"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            current_lesson = manager.current_lesson if manager else 1
        
        text = "📖 **Уроки**\n\n"
        buttons = []
        
        for i, lesson in enumerate(DEFAULT_LESSONS, 1):
            if i < current_lesson:
                status = "✅"
            elif i == current_lesson:
                status = "📖"
            else:
                status = "🔒"
            
            text += f"{status} Урок {i}: {lesson['title']}\n"
            
            if i <= current_lesson:
                buttons.append([InlineKeyboardButton(
                    text=f"{status} Урок {i}",
                    callback_data=f"lesson:{i}"
                )])
        
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_training")])
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in show_lessons: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data == "completed_lessons")
async def completed_lessons(callback: CallbackQuery):
    """Показать пройденные уроки"""
    await callback.answer()
    
    try:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            current_lesson = manager.current_lesson if manager else 1
        
        text = "✅ **Пройденные уроки**\n\n"
        buttons = []
        
        completed_count = current_lesson - 1
        if completed_count > 0:
            for i in range(1, min(completed_count + 1, len(DEFAULT_LESSONS) + 1)):
                lesson = DEFAULT_LESSONS[i - 1]
                text += f"✅ Урок {i}: {lesson['title']}\n"
                buttons.append([InlineKeyboardButton(
                    text=f"📖 Урок {i}",
                    callback_data=f"lesson:{i}"
                )])
        else:
            text += "_Вы ещё не прошли ни одного урока_"
        
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_training")])
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in completed_lessons: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data.startswith("lesson:"))
async def view_lesson(callback: CallbackQuery, state: FSMContext):
    """Просмотр урока"""
    await callback.answer()
    
    lesson_num = int(callback.data.split(":")[1])
    
    if lesson_num > len(DEFAULT_LESSONS):
        await callback.message.edit_text("❌ Урок не найден")
        return
    
    lesson = DEFAULT_LESSONS[lesson_num - 1]
    
    await callback.message.edit_text(
        lesson["content"],
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Пройти тест", callback_data=f"start_quiz:{lesson_num}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_training")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerStates.viewing_lesson)
    await state.update_data(current_lesson=lesson_num)


@router.callback_query(F.data.startswith("start_quiz:"))
async def start_quiz(callback: CallbackQuery, state: FSMContext):
    """Начать тест по уроку"""
    await callback.answer()
    
    lesson_num = int(callback.data.split(":")[1])
    lesson = DEFAULT_LESSONS[lesson_num - 1]
    
    if not lesson.get("quiz"):
        await callback.message.edit_text("📝 Тест для этого урока пока не готов")
        return
    
    await state.update_data(
        quiz_lesson=lesson_num,
        quiz_index=0,
        quiz_correct=0
    )
    
    await show_quiz_question(callback.message, state, lesson["quiz"], 0)
    await state.set_state(ManagerStates.taking_quiz)


async def show_quiz_question(message, state: FSMContext, quiz: list, index: int):
    """Показать вопрос теста"""
    if index >= len(quiz):
        data = await state.get_data()
        correct = data.get("quiz_correct", 0)
        total = len(quiz)
        lesson_num = data.get("quiz_lesson", 1)
        
        # Обновляем прогресс
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == message.chat.id)
            )
            manager = result.scalar_one_or_none()
            
            if manager and correct >= total // 2:  # Минимум 50% правильных
                if manager.current_lesson == lesson_num:
                    manager.current_lesson += 1
                    manager.training_score += correct * 10
                    await session.commit()
        
        passed = correct >= total // 2
        emoji = "🎉" if passed else "😔"
        
        await message.edit_text(
            f"{emoji} **Тест завершён!**\n\n"
            f"Правильных ответов: {correct}/{total}\n\n"
            f"{'✅ Урок засчитан!' if passed else '❌ Попробуйте ещё раз'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📖 К урокам", callback_data="show_lessons")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
        await state.clear()
        return
    
    question = quiz[index]
    buttons = []
    
    for i, option in enumerate(question["options"]):
        buttons.append([InlineKeyboardButton(
            text=option,
            callback_data=f"quiz_answer:{index}:{i}"
        )])
    
    await message.edit_text(
        f"❓ **Вопрос {index + 1}/{len(quiz)}**\n\n{question['question']}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data.startswith("quiz_answer:"), ManagerStates.taking_quiz)
async def quiz_answer(callback: CallbackQuery, state: FSMContext):
    """Ответ на вопрос теста"""
    await callback.answer()
    
    parts = callback.data.split(":")
    q_index = int(parts[1])
    answer = int(parts[2])
    
    data = await state.get_data()
    lesson_num = data.get("quiz_lesson", 1)
    lesson = DEFAULT_LESSONS[lesson_num - 1]
    quiz = lesson.get("quiz", [])
    
    if q_index < len(quiz):
        correct = quiz[q_index].get("correct", 0)
        if answer == correct:
            await state.update_data(quiz_correct=data.get("quiz_correct", 0) + 1)
    
    await state.update_data(quiz_index=q_index + 1)
    await show_quiz_question(callback.message, state, quiz, q_index + 1)


# ==================== ПРОГРЕСС ====================

@router.callback_query(F.data == "training_progress")
async def training_progress(callback: CallbackQuery):
    """Показать прогресс обучения"""
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
            
            first_name = manager.first_name or "Менеджер"
            training_score = manager.training_score or 0
            training_completed = manager.training_completed
            current_lesson = manager.current_lesson or 1
        
        lessons_text = ""
        for i, lesson in enumerate(DEFAULT_LESSONS, 1):
            if i < current_lesson:
                lessons_text += f"✅ Урок {i}: {lesson['title']}\n"
            elif i == current_lesson:
                lessons_text += f"📖 Урок {i}: {lesson['title']} ← текущий\n"
            else:
                lessons_text += f"🔒 Урок {i}: {lesson['title']}\n"
        
        status = "✅ Обучение пройдено" if training_completed else "📖 В процессе"
        
        await callback.message.edit_text(
            f"📊 **Мой прогресс**\n\n"
            f"👤 {first_name}\n"
            f"🏆 Баллы: {training_score}\n"
            f"📚 Статус: {status}\n\n"
            f"**Уроки:**\n{lessons_text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_training")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"Error in training_progress: {traceback.format_exc()}")
        await callback.message.answer(f"❌ Ошибка:\n`{str(e)}`", parse_mode=ParseMode.MARKDOWN)


# ==================== AI ТРЕНЕР ====================

@router.callback_query(F.data == "ai_trainer")
async def ai_trainer_start(callback: CallbackQuery, state: FSMContext):
    """Запустить AI-тренера"""
    await callback.answer()
    
    await callback.message.edit_text(
        "🤖 **AI-тренер**\n\n"
        "Я помогу вам с вопросами о продажах рекламы.\n\n"
        "Спросите меня о:\n"
        "• Работе с возражениями\n"
        "• Презентации каналов\n"
        "• Ценообразовании (CPM, ERR)\n"
        "• Техниках продаж\n\n"
        "Напишите ваш вопрос:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_training")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerStates.ai_conversation)


@router.message(ManagerStates.ai_conversation)
async def ai_trainer_message(message: Message, state: FSMContext):
    """Обработка сообщения для AI-тренера"""
    
    # Проверяем команды выхода
    if message.text and message.text.lower() in ["/cancel", "отмена", "выход"]:
        await state.clear()
        await message.answer(
            "👋 Диалог с AI-тренером завершён.",
            reply_markup=get_training_menu()
        )
        return
    
    # Получаем имя менеджера
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
        manager_name = manager.first_name if manager else "Менеджер"
    
    # Отправляем "печатает"
    await message.answer("🤔 Думаю...")
    
    # Получаем ответ от AI
    response = await ai_trainer_service.get_response(
        user_id=message.from_user.id,
        user_message=message.text,
        manager_name=manager_name
    )
    
    if response:
        await message.answer(
            response,
            reply_markup=get_ai_feedback_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.answer(
            "😔 Извините, не смог обработать запрос. Попробуйте ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_training")]
            ])
        )


@router.callback_query(F.data.startswith("ai_feedback:"))
async def ai_feedback(callback: CallbackQuery):
    """Обратная связь на ответ AI"""
    feedback = callback.data.split(":")[1]
    
    await ai_trainer_service.save_feedback(callback.from_user.id, feedback)
    
    if feedback == "helpful":
        await callback.answer("👍 Спасибо за отзыв!", show_alert=True)
    else:
        await callback.answer("Учту и постараюсь быть полезнее!", show_alert=True)

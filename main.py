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
    ForeignKey, Boolean, Numeric, JSON, Index, select, func, update, text
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.dialects.postgresql import JSONB

import os
import aiohttp

# ==================== КОНФИГУРАЦИЯ ====================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8309573885:AAEOEdMajLBLDKxvqNrcckxpPkSVSFtQ2ek")
ADMIN_IDS = [942180996]

# Get DATABASE_URL from environment
_raw_db_url = os.getenv("DATABASE_URL")
print(f"[DEBUG] Raw DATABASE_URL from env: {_raw_db_url[:50] if _raw_db_url else 'None'}...")

if _raw_db_url:
    # Railway gives postgresql:// but asyncpg needs postgresql+asyncpg://
    if _raw_db_url.startswith("postgres://"):
        DATABASE_URL = _raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif _raw_db_url.startswith("postgresql://") and "+asyncpg" not in _raw_db_url:
        DATABASE_URL = _raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    else:
        DATABASE_URL = _raw_db_url
else:
    DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/railway"

print(f"[DEBUG] Final DATABASE_URL: {DATABASE_URL[:50]}...")

SLOT_TIMES = [time(9, 0), time(18, 0)]
RESERVATION_MINUTES = 15

# TGStat API для аналитики охватов
TGSTAT_API_TOKEN = os.getenv("TGSTAT_API_TOKEN", "")  # Получить на tgstat.ru/api
TGSTAT_API_URL = "https://api.tgstat.ru"

# Telemetr API для аналитики (как Trustat)
TELEMETR_API_TOKEN = os.getenv("TELEMETR_API_TOKEN", "yeWKeyjhJkwAZCWkciIyDFfG5RVRYsIS")  # Получить через @telemetrio_api_bot
TELEMETR_API_URL = "https://api.telemetr.io"

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
    category = Column(String(100))  # Тематика канала
    # Цены по форматам размещения (JSON: {"1/24": 1000, "1/48": 800, "2/48": 1500, "native": 3000})
    prices = Column(JSON, default={"1/24": 0, "1/48": 0, "2/48": 0, "native": 0})
    # Аналитика охватов (как Trustat)
    subscribers = Column(Integer, default=0)  # Подписчики
    avg_reach = Column(Integer, default=0)  # Средний охват поста
    avg_reach_24h = Column(Integer, default=0)  # Охват за 24 часа
    avg_reach_48h = Column(Integer, default=0)  # Охват за 48 часов
    avg_reach_72h = Column(Integer, default=0)  # Охват за 72 часа
    err_percent = Column(Numeric(5, 2), default=0)  # ERR (вовлечённость)
    err24_percent = Column(Numeric(5, 2), default=0)  # ER24 (вовлечённость за 24ч)
    ci_index = Column(Numeric(8, 2), default=0)  # Индекс цитирования
    cpm = Column(Numeric(10, 2), default=0)  # Установленный CPM
    telemetr_id = Column(String(20))  # Internal ID в Telemetr.io
    analytics_updated = Column(DateTime)  # Когда обновлялась аналитика
    # Старые поля для совместимости
    price_morning = Column(Numeric(12, 2), default=0)
    price_evening = Column(Numeric(12, 2), default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    slots = relationship("Slot", back_populates="channel", cascade="all, delete-orphan")

# Тематики каналов с рекомендуемым CPM (руб/1000 просмотров)
CHANNEL_CATEGORIES = {
    # === Данные с Telega.in (актуальные) ===
    # Дорогие (от 3000₽)
    "real_estate": {"name": "🏠 Недвижимость", "cpm": 7061},
    "marketing": {"name": "📢 Маркетинг и PR", "cpm": 4680},
    "crypto": {"name": "🪙 Криптовалюты", "cpm": 4547},
    "business": {"name": "💼 Бизнес и стартапы", "cpm": 4823},
    "trading": {"name": "📈 Трейдинг", "cpm": 4163},
    "investments": {"name": "💰 Инвестиции", "cpm": 4058},
    "job_listings": {"name": "💼 Работа и вакансии", "cpm": 3651},
    "economy": {"name": "📊 Экономика и Финансы", "cpm": 3167},
    "jurisprudence": {"name": "⚖️ Юриспруденция", "cpm": 2947},
    "fashion": {"name": "👗 Мода и стиль", "cpm": 2828},
    
    # Средние (1500-3000₽)
    "it": {"name": "💻 Интернет технологии", "cpm": 2480},
    "culture": {"name": "🎭 Культура и события", "cpm": 2416},
    "blogs": {"name": "✍️ Блоги", "cpm": 2257},
    "education": {"name": "🎓 Образование", "cpm": 2249},
    "beauty": {"name": "💄 Красота и уход", "cpm": 2082},
    "kids": {"name": "👶 Дети и родители", "cpm": 2067},
    "travel": {"name": "✈️ Путешествия", "cpm": 2016},
    "fitness": {"name": "💪 Фитнес", "cpm": 2011},
    "science": {"name": "🔬 Наука и технологии", "cpm": 1824},
    "health": {"name": "🏥 Здоровье и медицина", "cpm": 1706},
    "art": {"name": "🎨 Искусство и дизайн", "cpm": 1698},
    "languages": {"name": "🌍 Иностранные языки", "cpm": 1605},
    "psychology": {"name": "🧠 Психология и отношения", "cpm": 1595},
    "music": {"name": "🎵 Музыка", "cpm": 1520},
    
    # Средние (1000-1500₽)
    "construction": {"name": "🏗 Строительство и ремонт", "cpm": 1461},
    "discounts": {"name": "🏷 Скидки и акции", "cpm": 1333},
    "religion": {"name": "🙏 Религия и духовность", "cpm": 1307},
    "auto": {"name": "🚗 Авто и мото", "cpm": 1232},
    "books": {"name": "📚 Книги и Подкасты", "cpm": 1241},
    "esoteric": {"name": "🔮 Эзотерика и Астрология", "cpm": 1193},
    "selfdev": {"name": "📈 Мотивация и саморазвитие", "cpm": 1170},
    "cooking": {"name": "🍳 Еда и кулинария", "cpm": 1047},
    "history": {"name": "📜 История", "cpm": 1025},
    "sport": {"name": "⚽ Спорт", "cpm": 1017},
    "news": {"name": "📰 Новости и СМИ", "cpm": 1005},
    "facts": {"name": "💡 Любопытные факты", "cpm": 945},
    "hobbies": {"name": "🎯 Хобби и деятельность", "cpm": 943},
    
    # Дешёвые (до 1000₽)
    "humor": {"name": "😂 Юмор и мемы", "cpm": 865},
    "animals": {"name": "🐱 Животные", "cpm": 861},
    "games": {"name": "🎮 Видеоигры", "cpm": 832},
    "cinema": {"name": "🎬 Кино", "cpm": 822},
    "celebrities": {"name": "⭐ Знаменитости", "cpm": 752},
    "entertainment": {"name": "🎉 Отдых и развлечения", "cpm": 639},
    "other": {"name": "📁 Другое", "cpm": 517},
    "uzbek": {"name": "🇺🇿 Узбекские каналы", "cpm": 452},
}

# Форматы размещения
PLACEMENT_FORMATS = {
    "1/24": {"name": "1/24", "hours": 24, "description": "Пост на 24 часа (удаляется)"},
    "1/48": {"name": "1/48", "hours": 48, "description": "Пост на 48 часов (удаляется)"},
    "2/48": {"name": "2/48", "hours": 48, "description": "2 поста на 48 часов"},
    "native": {"name": "Нативный", "hours": 0, "description": "Навсегда в канале"}
}

class CategoryCPM(Base):
    """Хранение актуальных CPM по тематикам (редактируется через бота)"""
    __tablename__ = "category_cpm"
    
    id = Column(Integer, primary_key=True)
    category_key = Column(String(50), unique=True, nullable=False)  # psychology, finance и т.д.
    name = Column(String(100), nullable=False)  # 🧠 Психология
    cpm = Column(Integer, default=0)  # CPM в рублях
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(BigInteger)  # Кто обновил

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
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=True)  # Кто привёл клиента
    status = Column(String(30), default="awaiting_payment")
    placement_format = Column(String(20), default="1/24")  # 1/24, 1/48, 2/48, native
    ad_content = Column(Text)
    ad_format = Column(String(20))  # text, photo, video
    ad_file_id = Column(String(255))
    final_price = Column(Numeric(12, 2), nullable=False)
    payment_screenshot_file_id = Column(String(255))
    delete_at = Column(DateTime)  # Когда удалить пост (для 1/24, 1/48)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    slot = relationship("Slot", back_populates="order")
    client = relationship("Client", back_populates="orders")
    manager = relationship("Manager", back_populates="orders")

# ==================== СИСТЕМА МЕНЕДЖЕРОВ ====================

class Manager(Base):
    """Менеджер по продажам"""
    __tablename__ = "managers"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    phone = Column(String(20))
    
    # Статус и уровень
    status = Column(String(20), default="trainee")  # trainee, active, senior, lead
    level = Column(Integer, default=1)  # 1-10
    experience_points = Column(Integer, default=0)
    
    # Финансы
    balance = Column(Numeric(12, 2), default=0)  # Текущий баланс для вывода
    total_earned = Column(Numeric(12, 2), default=0)  # Всего заработано
    commission_rate = Column(Numeric(5, 2), default=10)  # % от продаж (10-25%)
    
    # Статистика
    total_sales = Column(Integer, default=0)
    total_revenue = Column(Numeric(12, 2), default=0)
    clients_count = Column(Integer, default=0)
    
    # Обучение
    training_completed = Column(Boolean, default=False)
    training_score = Column(Integer, default=0)  # Баллы за тест
    current_lesson = Column(Integer, default=1)
    
    # Даты
    hired_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    
    # Связи
    orders = relationship("Order", back_populates="manager")
    achievements = relationship("ManagerAchievement", back_populates="manager")
    tasks = relationship("ManagerTask", back_populates="manager")

class ManagerAchievement(Base):
    """Достижения менеджера (бейджи)"""
    __tablename__ = "manager_achievements"
    
    id = Column(Integer, primary_key=True)
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=False)
    achievement_type = Column(String(50), nullable=False)
    earned_at = Column(DateTime, default=datetime.utcnow)
    
    manager = relationship("Manager", back_populates="achievements")

class ManagerTask(Base):
    """Задачи/цели для менеджера"""
    __tablename__ = "manager_tasks"
    
    id = Column(Integer, primary_key=True)
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=False)
    task_type = Column(String(50), nullable=False)  # daily, weekly, monthly, special
    title = Column(String(255), nullable=False)
    description = Column(Text)
    target_value = Column(Integer, default=1)  # Цель (например, 5 продаж)
    current_value = Column(Integer, default=0)  # Текущий прогресс
    reward_points = Column(Integer, default=0)  # XP за выполнение
    reward_money = Column(Numeric(12, 2), default=0)  # Бонус за выполнение
    status = Column(String(20), default="active")  # active, completed, expired
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    manager = relationship("Manager", back_populates="tasks")

class TrainingLesson(Base):
    """Уроки обучения"""
    __tablename__ = "training_lessons"
    
    id = Column(Integer, primary_key=True)
    lesson_number = Column(Integer, unique=True, nullable=False)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)  # Текст урока
    video_url = Column(String(500))  # Ссылка на видео
    quiz_questions = Column(JSON)  # Вопросы теста
    min_score = Column(Integer, default=70)  # Минимум для прохождения
    reward_points = Column(Integer, default=100)
    is_active = Column(Boolean, default=True)

class ManagerPayout(Base):
    """История выплат менеджерам"""
    __tablename__ = "manager_payouts"
    
    id = Column(Integer, primary_key=True)
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    status = Column(String(20), default="pending")  # pending, completed, rejected
    payment_method = Column(String(50))  # card, sbp, crypto
    payment_details = Column(String(255))  # Номер карты/телефона
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime)

# ==================== КОНФИГУРАЦИЯ МЕНЕДЖЕРОВ ====================

# Уровни менеджеров
MANAGER_LEVELS = {
    1: {"name": "Стажёр", "min_xp": 0, "commission": 10, "emoji": "🌱"},
    2: {"name": "Новичок", "min_xp": 500, "commission": 12, "emoji": "🌿"},
    3: {"name": "Продавец", "min_xp": 1500, "commission": 14, "emoji": "🌳"},
    4: {"name": "Опытный", "min_xp": 3500, "commission": 16, "emoji": "⭐"},
    5: {"name": "Профи", "min_xp": 7000, "commission": 18, "emoji": "🌟"},
    6: {"name": "Эксперт", "min_xp": 12000, "commission": 20, "emoji": "💫"},
    7: {"name": "Мастер", "min_xp": 20000, "commission": 22, "emoji": "🏆"},
    8: {"name": "Гуру", "min_xp": 35000, "commission": 24, "emoji": "👑"},
    9: {"name": "Легенда", "min_xp": 60000, "commission": 25, "emoji": "🔥"},
    10: {"name": "Топ-менеджер", "min_xp": 100000, "commission": 25, "emoji": "💎"},
}

# Достижения
ACHIEVEMENTS = {
    "first_sale": {"name": "Первая продажа", "emoji": "🎯", "xp": 100, "description": "Совершите первую продажу"},
    "sales_10": {"name": "10 продаж", "emoji": "🔟", "xp": 300, "description": "Совершите 10 продаж"},
    "sales_50": {"name": "50 продаж", "emoji": "5️⃣0️⃣", "xp": 1000, "description": "Совершите 50 продаж"},
    "sales_100": {"name": "Сотня!", "emoji": "💯", "xp": 3000, "description": "Совершите 100 продаж"},
    "revenue_10k": {"name": "10K оборот", "emoji": "💰", "xp": 500, "description": "Оборот 10 000₽"},
    "revenue_100k": {"name": "100K оборот", "emoji": "💎", "xp": 2000, "description": "Оборот 100 000₽"},
    "clients_5": {"name": "5 клиентов", "emoji": "👥", "xp": 200, "description": "Приведите 5 клиентов"},
    "clients_20": {"name": "20 клиентов", "emoji": "👨‍👩‍👧‍👦", "xp": 800, "description": "Приведите 20 клиентов"},
    "training_complete": {"name": "Выпускник", "emoji": "🎓", "xp": 500, "description": "Пройдите обучение"},
    "perfect_week": {"name": "Идеальная неделя", "emoji": "⚡", "xp": 400, "description": "Продажа каждый день недели"},
    "streak_7": {"name": "7 дней подряд", "emoji": "🔥", "xp": 350, "description": "Продажи 7 дней подряд"},
}

# Стандартные уроки обучения
DEFAULT_LESSONS = [
    {
        "lesson_number": 1,
        "title": "Введение в продажи рекламы",
        "content": """
📚 **Урок 1: Введение в продажи рекламы**

Добро пожаловать в команду! В этом уроке вы узнаете основы.

**Что такое реклама в Telegram?**
• Рекламодатели платят за размещение постов в каналах
• Форматы: 1/24, 1/48, 2/48, нативная реклама
• Цена зависит от охвата канала и формата

**Ваша задача:**
1. Находить рекламодателей
2. Консультировать по форматам
3. Закрывать сделки через бота
4. Получать комиссию от каждой продажи

**Важно помнить:**
• Клиент всегда прав
• Отвечайте быстро (в течение 5 минут)
• Будьте честны о возможностях

Переходите к следующему уроку! 👉
        """,
        "quiz_questions": [
            {"q": "Какой формат размещения подразумевает удаление поста через 24 часа?", "options": ["1/24", "1/48", "native"], "correct": 0},
            {"q": "В течение скольки минут нужно отвечать клиенту?", "options": ["15", "5", "30"], "correct": 1},
        ],
        "reward_points": 100
    },
    {
        "lesson_number": 2,
        "title": "Работа с клиентами",
        "content": """
📚 **Урок 2: Работа с клиентами**

**Где искать клиентов:**
• Чаты рекламодателей в Telegram
• Биржи рекламы (Telega.in, и др.)
• Рекомендации от текущих клиентов
• Холодные сообщения владельцам бизнеса

**Скрипт первого контакта:**
"Здравствуйте! Размещаем рекламу в каналах [тематика].
Охват от X до Y подписчиков.
Есть свободные слоты на эту неделю.
Интересно?"

**Работа с возражениями:**
• "Дорого" → Покажите стоимость за 1000 просмотров
• "Не уверен в результате" → Предложите тестовое размещение
• "Подумаю" → Уточните что смущает

**Правила общения:**
• Никогда не давите на клиента
• Предлагайте, а не навязывайте
• Будьте экспертом, не продавцом
        """,
        "quiz_questions": [
            {"q": "Что делать если клиент говорит 'дорого'?", "options": ["Снизить цену", "Показать стоимость за 1000 просмотров", "Закончить разговор"], "correct": 1},
            {"q": "Как часто нужно напоминать о себе потенциальному клиенту?", "options": ["Каждый час", "Раз в 2-3 дня", "Никогда"], "correct": 1},
        ],
        "reward_points": 150
    },
    {
        "lesson_number": 3,
        "title": "Форматы и ценообразование",
        "content": """
📚 **Урок 3: Форматы и ценообразование**

**Форматы размещения:**

📌 **1/24** — Пост удаляется через 24 часа
• Самый популярный формат
• Подходит для акций и срочных предложений
• Цена: базовая

📌 **1/48** — Пост удаляется через 48 часов
• Больше охват за счёт времени
• Цена: обычно +20-30% к 1/24

📌 **2/48** — Два поста за 48 часов
• Максимальный охват
• Первый пост + напоминание
• Цена: примерно 1.8x от 1/24

⭐ **Нативный** — Пост остаётся навсегда
• Для долгосрочных партнёров
• Вечный охват
• Цена: 3-5x от 1/24

**Как выбрать формат для клиента:**
1. Узнайте цель рекламы
2. Узнайте бюджет
3. Предложите оптимальный вариант
        """,
        "quiz_questions": [
            {"q": "Какой формат лучше для акции с дедлайном?", "options": ["native", "1/24", "2/48"], "correct": 1},
            {"q": "Во сколько раз дороже нативный формат?", "options": ["2x", "3-5x", "10x"], "correct": 1},
        ],
        "reward_points": 150
    },
    {
        "lesson_number": 4,
        "title": "Закрытие сделок",
        "content": """
📚 **Урок 4: Закрытие сделок**

**Сигналы готовности клиента:**
• Спрашивает о свободных датах
• Уточняет детали оплаты
• Говорит "в принципе интересно"

**Техники закрытия:**

1️⃣ **Прямое закрытие:**
"Отлично! Давайте оформим на завтра?"

2️⃣ **Альтернативное закрытие:**
"Вам удобнее разместиться в понедельник или среду?"

3️⃣ **Закрытие с дедлайном:**
"На эту неделю остался один слот, бронируем?"

**После согласия:**
1. Отправьте ссылку на бота
2. Помогите выбрать формат
3. Проконтролируйте оплату
4. Поблагодарите за сотрудничество

**Ваша комиссия** начисляется сразу после подтверждения оплаты!
        """,
        "quiz_questions": [
            {"q": "Какая техника: 'Вам на понедельник или среду?'", "options": ["Прямое закрытие", "Альтернативное закрытие", "С дедлайном"], "correct": 1},
            {"q": "Когда начисляется комиссия?", "options": ["Сразу после сделки", "После подтверждения оплаты", "В конце месяца"], "correct": 1},
        ],
        "reward_points": 200
    },
]

# Engine and session
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized")

async def migrate_db():
    """Добавляет новые колонки в существующие таблицы"""
    async with engine.begin() as conn:
        # Все колонки которые могут отсутствовать в channels
        channel_columns = [
            ("username", "VARCHAR(255)"),
            ("description", "TEXT"),
            ("prices", "JSON DEFAULT '{}'"),
            ("price_morning", "NUMERIC(12,2) DEFAULT 0"),
            ("price_evening", "NUMERIC(12,2) DEFAULT 0"),
            ("is_active", "BOOLEAN DEFAULT TRUE"),
            ("category", "VARCHAR(100)"),
            ("subscribers", "INTEGER DEFAULT 0"),
            ("avg_reach", "INTEGER DEFAULT 0"),
            ("avg_reach_24h", "INTEGER DEFAULT 0"),
            ("avg_reach_48h", "INTEGER DEFAULT 0"),
            ("avg_reach_72h", "INTEGER DEFAULT 0"),
            ("err_percent", "NUMERIC(5,2) DEFAULT 0"),
            ("err24_percent", "NUMERIC(5,2) DEFAULT 0"),
            ("ci_index", "NUMERIC(8,2) DEFAULT 0"),
            ("cpm", "NUMERIC(10,2) DEFAULT 0"),
            ("telemetr_id", "VARCHAR(20)"),
            ("analytics_updated", "TIMESTAMP"),
        ]
        
        for col_name, col_type in channel_columns:
            try:
                await conn.execute(
                    text(f"ALTER TABLE channels ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
                )
                logger.info(f"Added/checked column: channels.{col_name}")
            except Exception as e:
                logger.warning(f"Column channels.{col_name}: {e}")
        
        logger.info("Database migration completed")

async def init_category_cpm():
    """Инициализирует таблицу CPM из CHANNEL_CATEGORIES если она пустая"""
    async with async_session_maker() as session:
        # Проверяем есть ли записи
        result = await session.execute(select(func.count(CategoryCPM.id)))
        count = result.scalar()
        
        if count == 0:
            # Заполняем из CHANNEL_CATEGORIES
            for key, data in CHANNEL_CATEGORIES.items():
                cpm_entry = CategoryCPM(
                    category_key=key,
                    name=data["name"],
                    cpm=data["cpm"]
                )
                session.add(cpm_entry)
            await session.commit()
            logger.info(f"Initialized {len(CHANNEL_CATEGORIES)} CPM categories")
        else:
            logger.info(f"CPM categories already exist: {count} entries")

async def get_category_cpm(category_key: str) -> int:
    """Получить CPM для тематики из базы данных"""
    async with async_session_maker() as session:
        result = await session.execute(
            select(CategoryCPM).where(CategoryCPM.category_key == category_key)
        )
        cpm_entry = result.scalar_one_or_none()
        
        if cpm_entry:
            return cpm_entry.cpm
        
        # Fallback на CHANNEL_CATEGORIES
        return CHANNEL_CATEGORIES.get(category_key, {}).get("cpm", 500)

async def get_all_categories_cpm() -> dict:
    """Получить все тематики с CPM из базы"""
    async with async_session_maker() as session:
        result = await session.execute(select(CategoryCPM).order_by(CategoryCPM.cpm.desc()))
        categories = result.scalars().all()
        
        if categories:
            return {c.category_key: {"name": c.name, "cpm": c.cpm} for c in categories}
        
        # Fallback
        return CHANNEL_CATEGORIES

# ==================== СЕРВИС АНАЛИТИКИ TGSTAT ====================

class TGStatService:
    """Сервис для получения аналитики каналов через TGStat API"""
    
    def __init__(self, api_token: str):
        self.api_token = api_token
        self.base_url = TGSTAT_API_URL
    
    async def get_channel_stat(self, channel_username: str) -> Optional[dict]:
        """Получить статистику канала по username"""
        if not self.api_token:
            logger.warning("TGStat API token not configured")
            return None
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/channels/stat",
                    params={
                        "token": self.api_token,
                        "channelId": f"@{channel_username.lstrip('@')}"
                    }
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "ok":
                            return data.get("response", {})
                    logger.error(f"TGStat API error: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"TGStat API request failed: {e}")
            return None
    
    async def get_channel_by_id(self, telegram_id: int) -> Optional[dict]:
        """Получить информацию о канале по Telegram ID"""
        if not self.api_token:
            return None
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/channels/get",
                    params={
                        "token": self.api_token,
                        "channelId": str(telegram_id)
                    }
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "ok":
                            return data.get("response", {})
                    return None
        except Exception as e:
            logger.error(f"TGStat API request failed: {e}")
            return None

# Глобальный экземпляр сервиса
tgstat_service = TGStatService(TGSTAT_API_TOKEN)

# ==================== СЕРВИС АНАЛИТИКИ TELEMETR ====================

class TelemetrService:
    """Сервис для получения аналитики каналов через Telemetr API (как Trustat)"""
    
    def __init__(self, api_token: str):
        self.api_token = api_token
        self.base_url = TELEMETR_API_URL
    
    async def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Выполнить запрос к API"""
        if not self.api_token:
            logger.warning("Telemetr API token not configured")
            return None
        
        try:
            headers = {
                "x-api-key": self.api_token,
                "accept": "application/json"
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}{endpoint}",
                    headers=headers,
                    params=params
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 426:
                        logger.warning("Telemetr API quota reached")
                    else:
                        logger.error(f"Telemetr API error: {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Telemetr API request failed: {e}")
            return None
    
    async def resolve_telegram_id(self, telegram_id: int) -> Optional[str]:
        """Конвертировать Telegram ID в internal_id Telemetr"""
        # Убираем минус для каналов (Telegram даёт отрицательные ID)
        clean_id = abs(telegram_id)
        # Убираем префикс -100 если есть
        if clean_id > 1000000000000:
            clean_id = clean_id - 1000000000000
        
        data = await self._request("/v1/utils/resolve_telegram_id", {"telegram_id": clean_id})
        if data and "internal_id" in data:
            return data["internal_id"]
        return None
    
    async def search_channel(self, username: str) -> Optional[dict]:
        """Найти канал по username"""
        data = await self._request("/v1/channels/search", {"term": username.lstrip("@"), "limit": 1})
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return None
    
    async def get_channel_stats(self, internal_id: str) -> Optional[dict]:
        """Получить статистику канала по internal_id"""
        data = await self._request("/v1/channel/stats", {"internal_id": internal_id})
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return data
    
    async def get_channel_info(self, internal_id: str) -> Optional[dict]:
        """Получить информацию о канале по internal_id"""
        data = await self._request("/v1/channel/info", {"internal_id": internal_id})
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return data
    
    async def get_full_stats(self, telegram_id: int = None, username: str = None) -> Optional[dict]:
        """
        Получить полную статистику канала (как Trustat).
        
        Возвращает:
        {
            "internal_id": "xxx",
            "subscribers": 6384,
            "avg_views_24h": 527,
            "avg_views_48h": 638,
            "avg_views_72h": 780,  # Рассчитываем из avg_views
            "err_percent": 8.26,
            "err24_percent": 8.26,
            "title": "Пока муж не видит"
        }
        """
        internal_id = None
        
        # Пробуем найти internal_id
        if telegram_id:
            internal_id = await self.resolve_telegram_id(telegram_id)
        
        if not internal_id and username:
            channel = await self.search_channel(username)
            if channel:
                internal_id = channel.get("internal_id")
        
        if not internal_id:
            logger.warning(f"Could not find channel in Telemetr: tg_id={telegram_id}, username={username}")
            return None
        
        # Получаем статистику
        stats = await self.get_channel_stats(internal_id)
        if not stats:
            return None
        
        # Парсим данные
        avg_post_views = stats.get("avg_post_views", {})
        
        result = {
            "internal_id": internal_id,
            "title": stats.get("title", ""),
            "subscribers": stats.get("members_count", 0),
            "avg_views": avg_post_views.get("avg_post_views", 0),
            "avg_views_24h": avg_post_views.get("avg_post_views_24h", 0),
            "avg_views_48h": avg_post_views.get("avg_post_views_48h", 0),
            "avg_views_72h": avg_post_views.get("avg_post_views", 0),  # Используем общий как 72h
            "err_percent": stats.get("err_percent", 0),
            "err24_percent": stats.get("err24_percent", 0),
        }
        
        return result

# Глобальный экземпляр сервиса Telemetr
telemetr_service = TelemetrService(TELEMETR_API_TOKEN)

async def get_channel_stats_via_bot(bot: Bot, channel_id: int) -> Optional[dict]:
    """
    Получить статистику канала через Telegram Bot API.
    Бот должен быть админом канала.
    
    Returns:
        dict с полями: subscribers, title, username, description
        или None если нет доступа
    """
    try:
        # Получаем информацию о чате
        chat = await bot.get_chat(channel_id)
        
        # Получаем количество подписчиков
        members_count = await bot.get_chat_member_count(channel_id)
        
        return {
            "subscribers": members_count,
            "title": chat.title,
            "username": chat.username,
            "description": chat.description or "",
            "photo": chat.photo is not None,
        }
    except Exception as e:
        logger.warning(f"Cannot get stats for channel {channel_id}: {e}")
        return None

async def get_recent_posts_views(bot: Bot, channel_id: int, limit: int = 10) -> Optional[dict]:
    """
    Попытка получить просмотры последних постов.
    ВАЖНО: Bot API НЕ даёт доступ к просмотрам постов напрямую.
    Этот метод работает только если пересылать посты боту.
    
    Возвращает None — охваты нужно вводить вручную или через TGStat.
    """
    # К сожалению, Telegram Bot API не предоставляет доступ к просмотрам постов.
    # Для получения охватов нужен либо TGStat API, либо userbot через Telethon.
    return None

async def update_channel_from_bot(bot: Bot, channel_db_id: int) -> tuple[bool, str]:
    """
    Обновить статистику канала через Bot API.
    
    Returns:
        (success: bool, message: str)
    """
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_db_id))
        channel = result.scalar_one_or_none()
        
        if not channel:
            return False, "Канал не найден в базе"
        
        stats = await get_channel_stats_via_bot(bot, channel.telegram_id)
        
        if not stats:
            return False, "Нет доступа к каналу. Убедитесь что бот добавлен как администратор."
        
        # Обновляем данные
        await session.execute(
            update(Channel).where(Channel.id == channel_db_id).values(
                name=stats["title"],
                username=stats.get("username"),
                description=stats.get("description"),
                subscribers=stats["subscribers"],
                analytics_updated=datetime.utcnow()
            )
        )
        await session.commit()
        
        return True, f"Обновлено: {stats['subscribers']:,} подписчиков"

async def calculate_recommended_price_async(
    avg_reach: int,
    category: str,
    err_percent: float = 0,
    format_type: str = "1/24"
) -> int:
    """
    Рассчитать рекомендуемую цену размещения (асинхронная версия с CPM из БД)
    """
    # Получаем CPM из базы данных
    base_cpm = await get_category_cpm(category)
    
    # Базовая цена = (охват × CPM) / 1000
    base_price = (avg_reach * base_cpm) / 1000
    
    # Корректировка по ERR
    if err_percent > 20:
        base_price *= 1.3  # +30% за высокую вовлечённость
    elif err_percent > 15:
        base_price *= 1.15  # +15%
    elif err_percent < 10 and err_percent > 0:
        base_price *= 0.8  # -20% за низкую вовлечённость
    
    # Корректировка по формату
    format_multipliers = {
        "1/24": 1.0,
        "1/48": 0.8,  # -20% (дольше висит, но меньше охват в час)
        "2/48": 1.6,  # +60% (два поста)
        "native": 2.5  # +150% (навсегда)
    }
    base_price *= format_multipliers.get(format_type, 1.0)
    
    return int(base_price)

def calculate_recommended_price(
    avg_reach: int,
    category: str,
    err_percent: float = 0,
    format_type: str = "1/24",
    cpm_override: int = None
) -> int:
    """
    Рассчитать рекомендуемую цену размещения (синхронная версия)
    
    Args:
        avg_reach: Средний охват поста
        category: Тематика канала (ключ из CHANNEL_CATEGORIES)
        err_percent: ERR (вовлечённость) в процентах
        format_type: Формат размещения (1/24, 1/48, 2/48, native)
        cpm_override: Переопределить CPM (если передан)
    
    Returns:
        Рекомендуемая цена в рублях
    """
    # Базовый CPM по тематике
    if cpm_override:
        base_cpm = cpm_override
    else:
        category_data = CHANNEL_CATEGORIES.get(category, CHANNEL_CATEGORIES.get("other", {"cpm": 500}))
        base_cpm = category_data["cpm"]
    
    # Базовая цена = (охват × CPM) / 1000
    base_price = (avg_reach * base_cpm) / 1000
    
    # Корректировка по ERR
    if err_percent > 20:
        base_price *= 1.3  # +30% за высокую вовлечённость
    elif err_percent > 15:
        base_price *= 1.15  # +15%
    elif err_percent < 10 and err_percent > 0:
        base_price *= 0.8  # -20% за низкую вовлечённость
    
    # Корректировка по формату
    format_multipliers = {
        "1/24": 1.0,
        "1/48": 0.8,  # -20% (дольше висит, но меньше охват в час)
        "2/48": 1.6,  # +60% (два поста)
        "native": 2.5  # +150% (навсегда)
    }
    base_price *= format_multipliers.get(format_type, 1.0)
    
    return int(base_price)

def format_analytics_report(channel, recommended_prices: dict = None) -> str:
    """Форматировать отчёт по аналитике канала (как Trustat)"""
    
    lines = [
        f"📊 **Аналитика канала**",
        f"",
        f"📢 **{channel.name}**",
    ]
    
    if channel.username:
        lines.append(f"🔗 @{channel.username}")
    
    lines.append("")
    
    # Подписчики
    if channel.subscribers:
        lines.append(f"👥 Подписчики: **{channel.subscribers:,}**")
    
    # Охваты как в Trustat: 24ч | 48ч | 72ч
    views_parts = []
    if channel.avg_reach_24h:
        views_parts.append(f"24ч: {channel.avg_reach_24h:,}")
    if channel.avg_reach_48h:
        views_parts.append(f"48ч: {channel.avg_reach_48h:,}")
    if channel.avg_reach_72h:
        views_parts.append(f"72ч: {channel.avg_reach_72h:,}")
    elif channel.avg_reach:
        views_parts.append(f"avg: {channel.avg_reach:,}")
    
    if views_parts:
        lines.append(f"👁 Охваты: **{' | '.join(views_parts)}**")
    
    # ER24 как в Trustat
    if channel.err24_percent and float(channel.err24_percent) > 0:
        err = float(channel.err24_percent)
        err_emoji = "🔥" if err > 15 else "✅" if err > 10 else "⚠️"
        lines.append(f"{err_emoji} ER24: **{err:.2f}%**")
    elif channel.err_percent and float(channel.err_percent) > 0:
        err = float(channel.err_percent)
        err_emoji = "🔥" if err > 15 else "✅" if err > 10 else "⚠️"
        lines.append(f"{err_emoji} ER: **{err:.2f}%**")
    
    # Тематика и CPM
    if channel.category:
        cat_data = CHANNEL_CATEGORIES.get(channel.category, {})
        cat_name = cat_data.get("name", channel.category)
        cat_cpm = cat_data.get("cpm", 0)
        lines.append(f"🏷 **{cat_name}** (CPM: {cat_cpm:,}₽)")
    
    # Рекомендуемые цены
    if recommended_prices:
        lines.append(f"")
        lines.append(f"💡 **Рекомендуемые цены:**")
        for fmt, price in recommended_prices.items():
            if price > 0:
                lines.append(f"   • {fmt}: **{price:,}₽**")
    
    # Текущие цены
    if channel.prices:
        active_prices = {k: v for k, v in channel.prices.items() if v > 0}
        if active_prices:
            lines.append(f"")
            lines.append(f"✅ **Установленные цены:**")
            for fmt, price in active_prices.items():
                lines.append(f"   • {fmt}: **{price:,}₽**")
    
    # Дата обновления
    if channel.analytics_updated:
        lines.append(f"")
        lines.append(f"🕐 Обновлено: {channel.analytics_updated.strftime('%d.%m.%Y %H:%M')}")
    
    return "\n".join(filter(None, lines))

def format_analytics_short(channel) -> str:
    """Короткий формат аналитики для списка (как Trustat)"""
    parts = [f"**{channel.name}**"]
    
    # Подписчики
    if channel.subscribers:
        parts.append(f"👥 {channel.subscribers:,}")
    
    # Охваты 24/48/72
    views = []
    if channel.avg_reach_24h:
        views.append(f"24ч: {channel.avg_reach_24h:,}")
    if channel.avg_reach_48h:
        views.append(f"48ч: {channel.avg_reach_48h:,}")
    if channel.avg_reach_72h:
        views.append(f"72ч: {channel.avg_reach_72h:,}")
    if views:
        parts.append(f"👁 {' | '.join(views)}")
    
    # ER24
    if channel.err24_percent and float(channel.err24_percent) > 0:
        parts.append(f"📈 ER24: {float(channel.err24_percent):.2f}%")
    elif channel.err_percent and float(channel.err_percent) > 0:
        parts.append(f"📈 ER: {float(channel.err_percent):.2f}%")
    
    return " | ".join(parts)

# ==================== FSM СОСТОЯНИЯ ====================

class BookingStates(StatesGroup):
    selecting_channel = State()
    selecting_date = State()
    selecting_time = State()
    selecting_placement = State()  # Новый: выбор формата 1/24, 1/48 и т.д.
    selecting_format = State()  # Формат контента: text, photo, video
    waiting_content = State()
    confirming = State()
    waiting_payment = State()
    uploading_screenshot = State()

class AdminChannelStates(StatesGroup):
    waiting_channel_forward = State()
    waiting_channel_name = State()
    waiting_price_1_24 = State()
    waiting_price_1_48 = State()
    waiting_price_2_48 = State()
    waiting_price_native = State()
    # Аналитика
    waiting_category = State()
    waiting_manual_subscribers = State()
    waiting_manual_reach = State()
    waiting_manual_err = State()
    waiting_cpm = State()

class ManagerStates(StatesGroup):
    # Регистрация
    registration_phone = State()
    registration_confirm = State()
    # Обучение
    viewing_lesson = State()
    taking_quiz = State()
    # Вывод средств
    payout_amount = State()
    payout_method = State()
    payout_details = State()

# ==================== ФИЛЬТРЫ ====================

class IsAdmin(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in ADMIN_IDS

class IsManager(BaseFilter):
    """Проверка что пользователь — менеджер"""
    async def __call__(self, message: Message) -> bool:
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == message.from_user.id, Manager.is_active == True)
            )
            return result.scalar_one_or_none() is not None

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
        [KeyboardButton(text="📢 Каналы"), KeyboardButton(text="📊 Аналитика")],
        [KeyboardButton(text="💳 Оплаты"), KeyboardButton(text="📈 Статистика")],
        [KeyboardButton(text="◀️ Главное меню")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_channels_keyboard(channels: List[Channel]) -> InlineKeyboardMarkup:
    buttons = []
    for ch in channels:
        # Минимальная цена из всех форматов
        prices = ch.prices or {"1/24": 0}
        min_price = min(p for p in prices.values() if p > 0) if any(p > 0 for p in prices.values()) else 0
        buttons.append([InlineKeyboardButton(
            text=f"{ch.name} — от {min_price:,.0f}₽",
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

def get_times_keyboard(slots: List[Slot]) -> InlineKeyboardMarkup:
    """Клавиатура выбора времени (без цен — цены зависят от формата)"""
    buttons = []
    for slot in slots:
        emoji = "🌅" if slot.slot_time.hour < 12 else "🌆"
        time_str = slot.slot_time.strftime('%H:%M')
        buttons.append([InlineKeyboardButton(
            text=f"{emoji} {time_str}",
            callback_data=f"slot:{slot.id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_dates")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_placement_keyboard(channel: Channel) -> InlineKeyboardMarkup:
    """Клавиатура выбора формата размещения 1/24, 1/48 и т.д."""
    prices = channel.prices or {}
    buttons = []
    
    format_info = {
        "1/24": "📌 1/24 (на 24 часа)",
        "1/48": "📌 1/48 (на 48 часов)",
        "2/48": "📌 2/48 (2 поста на 48ч)",
        "native": "⭐ Навсегда"
    }
    
    for fmt, label in format_info.items():
        price = prices.get(fmt, 0)
        if price > 0:
            buttons.append([InlineKeyboardButton(
                text=f"{label} — {price:,.0f}₽",
                callback_data=f"placement:{fmt}"
            )])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_times")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_slots_keyboard(slots: List[Slot], channel: Channel) -> InlineKeyboardMarkup:
    """Старая клавиатура для совместимости — теперь используем get_times_keyboard"""
    return get_times_keyboard(slots)

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

# ==================== КЛАВИАТУРЫ ДЛЯ МЕНЕДЖЕРОВ ====================

def get_manager_menu() -> ReplyKeyboardMarkup:
    """Главное меню менеджера"""
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📊 Мой профиль"), KeyboardButton(text="💰 Баланс")],
        [KeyboardButton(text="📚 Обучение"), KeyboardButton(text="🎯 Задания")],
        [KeyboardButton(text="🏆 Достижения"), KeyboardButton(text="📈 Статистика")],
        [KeyboardButton(text="🔗 Моя ссылка")],
    ], resize_keyboard=True)

def get_training_keyboard(current_lesson: int, total_lessons: int) -> InlineKeyboardMarkup:
    """Клавиатура обучения"""
    buttons = []
    
    if current_lesson <= total_lessons:
        buttons.append([InlineKeyboardButton(
            text=f"📖 Урок {current_lesson}",
            callback_data=f"lesson:{current_lesson}"
        )])
    
    if current_lesson > 1:
        buttons.append([InlineKeyboardButton(
            text="📋 Пройденные уроки",
            callback_data="completed_lessons"
        )])
    
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="manager_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_quiz_keyboard(options: List[str], question_index: int) -> InlineKeyboardMarkup:
    """Клавиатура теста"""
    buttons = []
    for i, option in enumerate(options):
        buttons.append([InlineKeyboardButton(
            text=option,
            callback_data=f"quiz_answer:{question_index}:{i}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_payout_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура вывода средств"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 На карту", callback_data="payout:card")],
        [InlineKeyboardButton(text="📱 СБП", callback_data="payout:sbp")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="manager_back")],
    ])

def get_tasks_keyboard(tasks: list) -> InlineKeyboardMarkup:
    """Клавиатура заданий"""
    buttons = []
    for task in tasks:
        progress = f"{task.current_value}/{task.target_value}"
        emoji = "✅" if task.status == "completed" else "🎯"
        buttons.append([InlineKeyboardButton(
            text=f"{emoji} {task.title} ({progress})",
            callback_data=f"task_info:{task.id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="manager_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ==================== ХЕЛПЕРЫ ДЛЯ МЕНЕДЖЕРОВ ====================

async def get_manager_level(manager: Manager) -> dict:
    """Определяет текущий уровень менеджера по XP"""
    xp = manager.experience_points
    current_level = 1
    for level, data in MANAGER_LEVELS.items():
        if xp >= data["min_xp"]:
            current_level = level
    return MANAGER_LEVELS[current_level]

async def add_manager_xp(manager_id: int, xp: int, session: AsyncSession):
    """Добавляет XP менеджеру и проверяет повышение уровня"""
    manager = await session.get(Manager, manager_id)
    if not manager:
        return
    
    old_level = manager.level
    manager.experience_points += xp
    
    # Проверяем новый уровень
    for level, data in sorted(MANAGER_LEVELS.items(), reverse=True):
        if manager.experience_points >= data["min_xp"]:
            manager.level = level
            manager.commission_rate = Decimal(str(data["commission"]))
            break
    
    await session.commit()
    
    # Возвращаем True если был левел-ап
    return manager.level > old_level

async def check_achievements(manager_id: int, session: AsyncSession) -> List[str]:
    """Проверяет и выдаёт новые достижения"""
    manager = await session.get(Manager, manager_id)
    if not manager:
        return []
    
    # Получаем уже полученные достижения
    result = await session.execute(
        select(ManagerAchievement.achievement_type).where(ManagerAchievement.manager_id == manager_id)
    )
    earned = set(r[0] for r in result.fetchall())
    
    new_achievements = []
    
    # Проверяем каждое достижение
    checks = {
        "first_sale": manager.total_sales >= 1,
        "sales_10": manager.total_sales >= 10,
        "sales_50": manager.total_sales >= 50,
        "sales_100": manager.total_sales >= 100,
        "revenue_10k": float(manager.total_revenue) >= 10000,
        "revenue_100k": float(manager.total_revenue) >= 100000,
        "clients_5": manager.clients_count >= 5,
        "clients_20": manager.clients_count >= 20,
        "training_complete": manager.training_completed,
    }
    
    for achievement_type, condition in checks.items():
        if condition and achievement_type not in earned:
            # Выдаём достижение
            achievement = ManagerAchievement(
                manager_id=manager_id,
                achievement_type=achievement_type
            )
            session.add(achievement)
            
            # Начисляем XP
            xp = ACHIEVEMENTS[achievement_type]["xp"]
            manager.experience_points += xp
            
            new_achievements.append(achievement_type)
    
    if new_achievements:
        await session.commit()
    
    return new_achievements

async def notify_new_achievement(bot: Bot, manager: Manager, achievement_type: str):
    """Уведомляет менеджера о новом достижении"""
    ach = ACHIEVEMENTS.get(achievement_type, {})
    try:
        await bot.send_message(
            manager.telegram_id,
            f"🎉 **Новое достижение!**\n\n"
            f"{ach.get('emoji', '🏆')} **{ach.get('name', achievement_type)}**\n"
            f"{ach.get('description', '')}\n\n"
            f"+{ach.get('xp', 0)} XP",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass

# ==================== РОУТЕРЫ ====================

router = Router()

# --- Команда /start ---
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    
    # Проверяем реферальную ссылку
    args = message.text.split()
    ref_manager_id = None
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_manager_id = int(args[1].replace("ref_", ""))
            # Сохраняем в состояние для будущих заказов
            await state.update_data(ref_manager_id=ref_manager_id)
        except:
            pass
    
    is_admin = message.from_user.id in ADMIN_IDS
    
    # Проверяем, является ли пользователь менеджером
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if manager:
        role = "менеджер"
        extra_text = "\n\n💼 Для панели менеджера: /manager"
    elif is_admin:
        role = "администратор"
        extra_text = ""
    else:
        role = "клиент"
        extra_text = ""
        if ref_manager_id:
            extra_text = "\n\n✨ Вы пришли по приглашению нашего менеджера!"
    
    await message.answer(
        f"👋 Добро пожаловать в CRM-бот!\n\n"
        f"Здесь вы можете забронировать рекламу в наших каналах.\n\n"
        f"🔑 Ваша роль: **{role}**{extra_text}",
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
    
    await state.update_data(slot_id=slot_id, slot_time=slot.slot_time.strftime('%H:%M'))
    
    await callback.message.edit_text(
        f"✅ Слот зарезервирован на {RESERVATION_MINUTES} минут!\n\n"
        f"📌 **Выберите формат размещения:**",
        reply_markup=get_placement_keyboard(channel),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(BookingStates.selecting_placement)

# --- Кнопка назад к выбору времени ---
@router.callback_query(F.data == "back_to_times")
async def back_to_times(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    
    # Освобождаем зарезервированный слот
    if "slot_id" in data:
        async with async_session_maker() as session:
            slot = await session.get(Slot, data["slot_id"])
            if slot and slot.status == "reserved":
                slot.status = "available"
                slot.reserved_by = None
                slot.reserved_until = None
                await session.commit()
    
    # Возвращаемся к выбору времени
    channel_id = data.get("channel_id")
    date_str = data.get("selected_date")
    
    if channel_id and date_str:
        selected_date = date.fromisoformat(date_str)
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
        
        await callback.message.edit_text(
            f"📅 **{selected_date.strftime('%d.%m.%Y')}**\n\n"
            f"Выберите время:",
            reply_markup=get_times_keyboard(slots),
            parse_mode=ParseMode.MARKDOWN
        )
        await state.set_state(BookingStates.selecting_time)
    else:
        await callback.message.edit_text("❌ Ошибка. Начните заново с /start")

# --- Выбор формата размещения (1/24, 1/48 и т.д.) ---
@router.callback_query(F.data.startswith("placement:"), BookingStates.selecting_placement)
async def select_placement(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    placement = callback.data.split(":")[1]  # 1/24, 1/48, 2/48, native
    
    data = await state.get_data()
    channel_id = data.get("channel_id")
    
    async with async_session_maker() as session:
        channel = await session.get(Channel, channel_id)
        prices = channel.prices or {}
        price = prices.get(placement, 0)
    
    await state.update_data(placement_format=placement, price=float(price))
    
    placement_names = {
        "1/24": "1/24 (24 часа)",
        "1/48": "1/48 (48 часов)",
        "2/48": "2/48 (2 поста)",
        "native": "Навсегда"
    }
    
    await callback.message.edit_text(
        f"📌 Формат: **{placement_names.get(placement, placement)}** — {price:,.0f}₽\n\n"
        f"Выберите тип контента:",
        reply_markup=get_format_keyboard(),
        parse_mode=ParseMode.MARKDOWN
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
    slot_time = data.get("slot_time", "")
    placement_format = data.get("placement_format", "1/24")
    
    placement_names = {
        "1/24": "1/24 (на 24 часа)",
        "1/48": "1/48 (на 48 часов)",
        "2/48": "2/48 (2 поста)",
        "native": "Навсегда"
    }
    
    await message.answer(
        f"📋 **Подтверждение заказа**\n\n"
        f"📢 Канал: {channel_name}\n"
        f"📅 Дата: {selected_date}\n"
        f"🕐 Время: {slot_time}\n"
        f"📌 Размещение: {placement_names.get(placement_format, placement_format)}\n"
        f"📝 Контент: {ad_format}\n"
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
        
        # Вычисляем время удаления поста
        placement = data.get("placement_format", "1/24")
        delete_at = None
        if placement in PLACEMENT_FORMATS:
            hours = PLACEMENT_FORMATS[placement]["hours"]
            if hours > 0:
                delete_at = datetime.utcnow() + timedelta(hours=hours)
        
        # Проверяем реферальную ссылку менеджера
        ref_manager_id = data.get("ref_manager_id")
        manager_id = None
        if ref_manager_id:
            manager_result = await session.execute(
                select(Manager).where(Manager.id == ref_manager_id, Manager.is_active == True)
            )
            manager = manager_result.scalar_one_or_none()
            if manager:
                manager_id = manager.id
        
        # Создаём заказ
        order = Order(
            slot_id=data["slot_id"],
            client_id=client.id,
            manager_id=manager_id,
            placement_format=placement,
            ad_content=data.get("ad_content"),
            ad_format=data["ad_format"],
            ad_file_id=data.get("ad_file_id"),
            final_price=Decimal(str(data["price"])),
            delete_at=delete_at
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
            prices = ch.prices or {}
            price_str = " | ".join([f"{k}: {v:,.0f}₽" for k, v in prices.items() if v > 0])
            if not price_str:
                price_str = "💰 Цены не установлены"
            text += f"{status} **{ch.name}** (ID: {ch.id})\n   {price_str}\n\n"
    else:
        text = "📢 Каналов пока нет\n\n"
    
    text += "➕ Добавить: /add\\_channel\n"
    text += "💰 Установить цены: /set\\_prices <ID>"
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
async def receive_channel_forward(message: Message, state: FSMContext, bot: Bot):
    logger.info(f"[ADD_CHANNEL] Received message from {message.from_user.id}")
    
    if not message.forward_from_chat:
        logger.info(f"[ADD_CHANNEL] No forward_from_chat - message type: {message.content_type}")
        await message.answer("❌ Перешлите сообщение из канала")
        return
    
    chat = message.forward_from_chat
    logger.info(f"[ADD_CHANNEL] Forward from chat: {chat.id} - {chat.title}")
    
    # Сразу сохраняем канал с нулевыми ценами
    try:
        # Пробуем получить статистику если бот админ
        bot_stats = await get_channel_stats_via_bot(bot, chat.id)
        subscribers = bot_stats["subscribers"] if bot_stats else 0
        
        async with async_session_maker() as session:
            # Проверяем, не добавлен ли уже
            existing = await session.execute(
                select(Channel).where(Channel.telegram_id == chat.id)
            )
            if existing.scalar_one_or_none():
                logger.info(f"[ADD_CHANNEL] Channel already exists: {chat.id}")
                await message.answer(
                    f"❌ Канал **{chat.title}** уже добавлен!",
                    reply_markup=get_admin_menu(),
                    parse_mode=ParseMode.MARKDOWN
                )
                await state.clear()
                return
            
            channel = Channel(
                telegram_id=chat.id,
                name=chat.title,
                username=chat.username,
                subscribers=subscribers,
                prices={"1/24": 0, "1/48": 0, "2/48": 0, "native": 0},
                analytics_updated=datetime.utcnow() if bot_stats else None
            )
            session.add(channel)
            await session.flush()
            logger.info(f"[ADD_CHANNEL] Channel created with id: {channel.id}")
            
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
            channel_id = channel.id
            logger.info(f"[ADD_CHANNEL] Success! Channel ID: {channel_id}, slots created")
        
        # Формируем ответ
        stats_info = ""
        if bot_stats:
            stats_info = f"👥 Подписчиков: **{subscribers:,}**\n"
            stats_info += "✅ Бот имеет доступ к статистике\n\n"
        else:
            stats_info = "⚠️ Бот не админ — добавьте для авто-статистики\n\n"
        
        await message.answer(
            f"✅ **Канал добавлен!**\n\n"
            f"📢 {chat.title}\n"
            f"🆔 ID: {channel_id}\n"
            f"{stats_info}"
            f"📅 Создано 60 слотов\n\n"
            f"**Следующие шаги:**\n"
            f"• /analytics {channel_id} — посмотреть статистику\n"
            f"• /set\\_category {channel_id} — выбрать тематику\n"
            f"• /set\\_prices {channel_id} — установить цены",
            reply_markup=get_admin_menu(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.error(f"[ADD_CHANNEL] Error: {e}", exc_info=True)
        await message.answer(
            f"❌ Ошибка при добавлении канала:\n`{str(e)[:200]}`",
            parse_mode=ParseMode.MARKDOWN
        )
    
    await state.clear()

# --- Установка цен канала ---
@router.message(Command("set_prices"), IsAdmin())
async def start_set_prices(message: Message, state: FSMContext):
    args = message.text.split()
    if len(args) < 2:
        # Показываем список каналов
        async with async_session_maker() as session:
            result = await session.execute(select(Channel).where(Channel.is_active == True))
            channels = result.scalars().all()
        
        if not channels:
            await message.answer("❌ Нет каналов")
            return
        
        text = "📢 **Выберите канал для установки цен:**\n\n"
        for ch in channels:
            prices = ch.prices or {}
            price_str = " | ".join([f"{k}: {v}₽" for k, v in prices.items() if v > 0]) or "не установлены"
            text += f"• **{ch.name}** (ID: {ch.id})\n  💰 {price_str}\n\n"
        text += "Используйте: /set\\_prices <ID>"
        
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)
        return
    
    try:
        channel_id = int(args[1])
    except:
        await message.answer("❌ Неверный ID канала")
        return
    
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    if not channel:
        await message.answer("❌ Канал не найден")
        return
    
    await state.update_data(price_channel_id=channel_id, price_channel_name=channel.name)
    await message.answer(
        f"💰 **Установка цен для {channel.name}**\n\n"
        f"📌 Введите цену за формат **1/24** (пост на 24 часа):\n"
        f"(введите 0 если формат не нужен)",
        reply_markup=get_cancel_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminChannelStates.waiting_price_1_24)

@router.message(AdminChannelStates.waiting_price_1_24)
async def receive_price_1_24(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except:
        await message.answer("❌ Введите число")
        return
    
    await state.update_data(price_1_24=price)
    await message.answer(
        "📌 Введите цену за формат **1/48** (пост на 48 часов):\n"
        "(введите 0 если не нужен)",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminChannelStates.waiting_price_1_48)

@router.message(AdminChannelStates.waiting_price_1_48)
async def receive_price_1_48(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except:
        await message.answer("❌ Введите число")
        return
    
    await state.update_data(price_1_48=price)
    await message.answer(
        "📌 Введите цену за формат **2/48** (2 поста на 48 часов):\n"
        "(введите 0 если не нужен)",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminChannelStates.waiting_price_2_48)

@router.message(AdminChannelStates.waiting_price_2_48)
async def receive_price_2_48(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except:
        await message.answer("❌ Введите число")
        return
    
    await state.update_data(price_2_48=price)
    await message.answer(
        "📌 Введите цену за **нативный** формат (навсегда):\n"
        "(введите 0 если не нужен)",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(AdminChannelStates.waiting_price_native)

@router.message(AdminChannelStates.waiting_price_native)
async def receive_price_native(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except:
        await message.answer("❌ Введите число")
        return
    
    data = await state.get_data()
    
    prices = {
        "1/24": data.get("price_1_24", 0),
        "1/48": data.get("price_1_48", 0),
        "2/48": data.get("price_2_48", 0),
        "native": price
    }
    
    channel_id = data.get("price_channel_id")
    channel_name = data.get("price_channel_name", "Канал")
    
    async with async_session_maker() as session:
        await session.execute(
            update(Channel).where(Channel.id == channel_id).values(prices=prices)
        )
        await session.commit()
    
    price_str = " | ".join([f"{k}: {v:,.0f}₽" for k, v in prices.items() if v > 0]) or "все форматы отключены"
    
    await message.answer(
        f"✅ **Цены обновлены!**\n\n"
        f"📢 {channel_name}\n"
        f"💰 {price_str}",
        reply_markup=get_admin_menu(),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.clear()

# --- Аналитика каналов ---
@router.message(Command("analytics"), IsAdmin())
async def cmd_analytics(message: Message, state: FSMContext):
    """Показать меню аналитики"""
    args = message.text.split()
    
    if len(args) < 2:
        # Показываем список каналов
        async with async_session_maker() as session:
            result = await session.execute(select(Channel).where(Channel.is_active == True))
            channels = result.scalars().all()
        
        if not channels:
            await message.answer("❌ Нет каналов")
            return
        
        text = "📊 **Аналитика каналов**\n\n"
        for ch in channels:
            subs = f"{ch.subscribers:,}" if ch.subscribers else "—"
            reach = f"{ch.avg_reach:,}" if ch.avg_reach else "—"
            err = f"{float(ch.err_percent):.1f}%" if ch.err_percent else "—"
            cat = CHANNEL_CATEGORIES.get(ch.category, {}).get("name", "—") if ch.category else "—"
            text += f"• **{ch.name}** (ID: {ch.id})\n"
            text += f"  👥 {subs} | 👁 {reach} | ERR: {err} | 🏷 {cat}\n\n"
        
        text += "**Команды:**\n"
        text += "/analytics <ID> — детали канала\n"
        text += "/update\\_stats <ID> — обновить через TGStat\n"
        text += "/set\\_category <ID> — установить тематику\n"
        text += "/manual\\_stats <ID> — ввести данные вручную"
        
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)
        return
    
    # Показываем детали канала
    try:
        channel_id = int(args[1])
    except:
        await message.answer("❌ Неверный ID")
        return
    
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    if not channel:
        await message.answer("❌ Канал не найден")
        return
    
    # Рассчитываем рекомендуемые цены
    recommended = {}
    if channel.avg_reach and channel.category:
        for fmt in ["1/24", "1/48", "2/48", "native"]:
            recommended[fmt] = calculate_recommended_price(
                channel.avg_reach,
                channel.category,
                float(channel.err_percent or 0),
                fmt
            )
    
    report = format_analytics_report(channel, recommended)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Обновить TGStat", callback_data=f"update_stats:{channel_id}"),
            InlineKeyboardButton(text="✏️ Ввести вручную", callback_data=f"manual_stats:{channel_id}")
        ],
        [
            InlineKeyboardButton(text="🏷 Тематика", callback_data=f"set_category:{channel_id}"),
            InlineKeyboardButton(text="💰 Установить цены", callback_data=f"apply_prices:{channel_id}")
        ]
    ])
    
    await message.answer(report, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("update_stats:"), IsAdmin())
async def cb_update_stats(callback: CallbackQuery, bot: Bot):
    """Обновить статистику — Bot API + Telemetr (как Trustat)"""
    channel_id = int(callback.data.split(":")[1])
    
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    if not channel:
        await callback.answer("Канал не найден", show_alert=True)
        return
    
    await callback.answer("⏳ Загружаю статистику...")
    
    update_data = {"analytics_updated": datetime.utcnow()}
    source_parts = []
    
    # 1. Получаем подписчиков через Bot API (если бот админ)
    bot_stats = await get_channel_stats_via_bot(bot, channel.telegram_id)
    if bot_stats:
        update_data["name"] = bot_stats["title"]
        update_data["username"] = bot_stats.get("username")
        update_data["subscribers"] = bot_stats["subscribers"]
        source_parts.append("Bot API")
    
    # 2. Получаем охваты через Telemetr API (как Trustat)
    telemetr_stats = None
    if TELEMETR_API_TOKEN:
        telemetr_stats = await telemetr_service.get_full_stats(
            telegram_id=channel.telegram_id,
            username=channel.username or (bot_stats.get("username") if bot_stats else None)
        )
        
        if telemetr_stats:
            update_data["telemetr_id"] = telemetr_stats.get("internal_id")
            update_data["avg_reach"] = telemetr_stats.get("avg_views", 0)
            update_data["avg_reach_24h"] = telemetr_stats.get("avg_views_24h", 0)
            update_data["avg_reach_48h"] = telemetr_stats.get("avg_views_48h", 0)
            update_data["avg_reach_72h"] = telemetr_stats.get("avg_views_72h", 0)
            update_data["err_percent"] = telemetr_stats.get("err_percent", 0)
            update_data["err24_percent"] = telemetr_stats.get("err24_percent", 0)
            
            # Если не получили подписчиков через Bot API
            if "subscribers" not in update_data and telemetr_stats.get("subscribers"):
                update_data["subscribers"] = telemetr_stats["subscribers"]
            
            source_parts.append("Telemetr")
    
    # 3. Fallback на TGStat если нет Telemetr
    if not telemetr_stats and TGSTAT_API_TOKEN:
        username = update_data.get("username") or channel.username
        if username:
            tgstat_stats = await tgstat_service.get_channel_stat(username)
            if tgstat_stats:
                update_data["avg_reach"] = tgstat_stats.get("avg_post_reach", 0)
                update_data["avg_reach_24h"] = tgstat_stats.get("adv_post_reach_24h", 0)
                update_data["err_percent"] = tgstat_stats.get("err_percent", 0)
                source_parts.append("TGStat")
    
    # Сохраняем данные
    if update_data:
        async with async_session_maker() as session:
            await session.execute(
                update(Channel).where(Channel.id == channel_id).values(**update_data)
            )
            await session.commit()
    
    # Перечитываем для отчёта
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    # Формируем отчёт
    recommended = {}
    reach_for_calc = channel.avg_reach_24h or channel.avg_reach
    if reach_for_calc and channel.category:
        for fmt in ["1/24", "1/48", "2/48", "native"]:
            recommended[fmt] = calculate_recommended_price(
                reach_for_calc,
                channel.category,
                float(channel.err_percent or 0),
                fmt
            )
    
    source = " + ".join(source_parts) if source_parts else "нет данных"
    report = f"✅ **Данные обновлены!** ({source})\n\n" + format_analytics_report(channel, recommended)
    
    # Предупреждение если нет охватов
    if not channel.avg_reach_24h and not channel.avg_reach:
        if not TELEMETR_API_TOKEN:
            report += "\n\n⚠️ _Для охватов добавьте TELEMETR\\_API\\_TOKEN (получить: @telemetrio\\_api\\_bot)_"
        else:
            report += "\n\n⚠️ _Охваты не найдены. Введите вручную._"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏷 Тематика", callback_data=f"set_category:{channel_id}"),
            InlineKeyboardButton(text="✏️ Ввести охват", callback_data=f"manual_stats:{channel_id}")
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"update_stats:{channel_id}"),
            InlineKeyboardButton(text="💰 Применить цены", callback_data=f"apply_prices:{channel_id}")
        ]
    ])
    
    await callback.message.edit_text(report, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("set_category:"), IsAdmin())
async def cb_set_category(callback: CallbackQuery, state: FSMContext):
    """Выбрать тематику канала"""
    channel_id = int(callback.data.split(":")[1])
    
    # Получаем категории из базы данных
    categories = await get_all_categories_cpm()
    
    # Сортируем по CPM (дорогие сверху)
    sorted_cats = sorted(categories.items(), key=lambda x: x[1]["cpm"], reverse=True)
    
    # Создаём клавиатуру с категориями
    buttons = []
    row = []
    for key, data in sorted_cats:
        row.append(InlineKeyboardButton(
            text=f"{data['name']} ({data['cpm']}₽)",
            callback_data=f"category:{channel_id}:{key}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(
        "🏷 **Выберите тематику канала:**\n\n"
        "(в скобках указан CPM — редактируется через /cpm)",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@router.callback_query(F.data.startswith("category:"), IsAdmin())
async def cb_category_selected(callback: CallbackQuery):
    """Сохранить выбранную тематику"""
    parts = callback.data.split(":")
    channel_id = int(parts[1])
    category = parts[2]
    
    async with async_session_maker() as session:
        await session.execute(
            update(Channel).where(Channel.id == channel_id).values(category=category)
        )
        await session.commit()
        
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    cat_name = CHANNEL_CATEGORIES.get(category, {}).get("name", category)
    
    # Рассчитываем рекомендуемые цены
    recommended = {}
    if channel.avg_reach:
        for fmt in ["1/24", "1/48", "2/48", "native"]:
            recommended[fmt] = calculate_recommended_price(
                channel.avg_reach,
                category,
                float(channel.err_percent or 0),
                fmt
            )
    
    report = f"✅ Тематика установлена: **{cat_name}**\n\n" + format_analytics_report(channel, recommended)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Применить рекомендуемые цены", callback_data=f"apply_prices:{channel_id}")]
    ])
    
    await callback.message.edit_text(report, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data.startswith("apply_prices:"), IsAdmin())
async def cb_apply_prices(callback: CallbackQuery):
    """Применить рекомендуемые цены к каналу"""
    channel_id = int(callback.data.split(":")[1])
    
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    if not channel:
        await callback.answer("Канал не найден", show_alert=True)
        return
    
    if not channel.avg_reach or not channel.category:
        await callback.answer("Сначала укажите охват и тематику канала", show_alert=True)
        return
    
    # Рассчитываем цены
    new_prices = {}
    for fmt in ["1/24", "1/48", "2/48", "native"]:
        new_prices[fmt] = calculate_recommended_price(
            channel.avg_reach,
            channel.category,
            float(channel.err_percent or 0),
            fmt
        )
    
    # Сохраняем
    async with async_session_maker() as session:
        await session.execute(
            update(Channel).where(Channel.id == channel_id).values(prices=new_prices)
        )
        await session.commit()
    
    price_str = "\n".join([f"• {k}: **{v:,}₽**" for k, v in new_prices.items()])
    
    await callback.message.edit_text(
        f"✅ **Цены применены!**\n\n"
        f"📢 {channel.name}\n\n"
        f"{price_str}",
        parse_mode=ParseMode.MARKDOWN
    )

@router.callback_query(F.data.startswith("manual_stats:"), IsAdmin())
async def cb_manual_stats(callback: CallbackQuery, state: FSMContext):
    """Начать ввод статистики вручную"""
    channel_id = int(callback.data.split(":")[1])
    
    await state.update_data(manual_channel_id=channel_id)
    await state.set_state(AdminChannelStates.waiting_manual_subscribers)
    
    await callback.message.edit_text(
        "✏️ **Ввод статистики вручную**\n\n"
        "Введите количество подписчиков:",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(Command("manual_stats"), IsAdmin())
async def cmd_manual_stats(message: Message, state: FSMContext):
    """Команда для ввода статистики вручную"""
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /manual\\_stats <ID канала>", parse_mode=ParseMode.MARKDOWN)
        return
    
    try:
        channel_id = int(args[1])
    except:
        await message.answer("❌ Неверный ID")
        return
    
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    if not channel:
        await message.answer("❌ Канал не найден")
        return
    
    await state.update_data(manual_channel_id=channel_id)
    await state.set_state(AdminChannelStates.waiting_manual_subscribers)
    
    await message.answer(
        f"✏️ **Ввод статистики для {channel.name}**\n\n"
        "Введите количество подписчиков:",
        reply_markup=get_cancel_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(AdminChannelStates.waiting_manual_subscribers)
async def receive_manual_subscribers(message: Message, state: FSMContext):
    try:
        subscribers = int(message.text.strip().replace(" ", "").replace(",", ""))
    except:
        await message.answer("❌ Введите число")
        return
    
    await state.update_data(manual_subscribers=subscribers)
    await state.set_state(AdminChannelStates.waiting_manual_reach)
    
    await message.answer(
        f"✅ Подписчики: {subscribers:,}\n\n"
        "Введите средний охват поста:",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(AdminChannelStates.waiting_manual_reach)
async def receive_manual_reach(message: Message, state: FSMContext):
    try:
        reach = int(message.text.strip().replace(" ", "").replace(",", ""))
    except:
        await message.answer("❌ Введите число")
        return
    
    await state.update_data(manual_reach=reach)
    await state.set_state(AdminChannelStates.waiting_manual_err)
    
    await message.answer(
        f"✅ Охват: {reach:,}\n\n"
        "Введите ERR (вовлечённость) в процентах (например: 15):\n"
        "(или 0 если не знаете)",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(AdminChannelStates.waiting_manual_err)
async def receive_manual_err(message: Message, state: FSMContext):
    try:
        err = float(message.text.strip().replace(",", ".").replace("%", ""))
    except:
        await message.answer("❌ Введите число")
        return
    
    data = await state.get_data()
    channel_id = data["manual_channel_id"]
    subscribers = data["manual_subscribers"]
    reach = data["manual_reach"]
    
    # Сохраняем данные
    async with async_session_maker() as session:
        await session.execute(
            update(Channel).where(Channel.id == channel_id).values(
                subscribers=subscribers,
                avg_reach=reach,
                avg_reach_24h=reach,
                err_percent=err,
                analytics_updated=datetime.utcnow()
            )
        )
        await session.commit()
        
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    await state.clear()
    
    # Формируем отчёт
    recommended = {}
    if channel.category:
        for fmt in ["1/24", "1/48", "2/48", "native"]:
            recommended[fmt] = calculate_recommended_price(reach, channel.category, err, fmt)
    
    report = "✅ **Статистика сохранена!**\n\n" + format_analytics_report(channel, recommended)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏷 Тематика", callback_data=f"set_category:{channel_id}"),
            InlineKeyboardButton(text="💰 Применить цены", callback_data=f"apply_prices:{channel_id}")
        ]
    ])
    
    await message.answer(report, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("set_category"), IsAdmin())
async def cmd_set_category(message: Message, state: FSMContext):
    """Команда для установки тематики"""
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /set\\_category <ID канала>", parse_mode=ParseMode.MARKDOWN)
        return
    
    try:
        channel_id = int(args[1])
    except:
        await message.answer("❌ Неверный ID")
        return
    
    # Создаём клавиатуру с категориями
    buttons = []
    row = []
    for key, data in CHANNEL_CATEGORIES.items():
        row.append(InlineKeyboardButton(
            text=f"{data['name']} ({data['cpm']}₽)",
            callback_data=f"category:{channel_id}:{key}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await message.answer(
        "🏷 **Выберите тематику канала:**\n\n"
        "(в скобках указан средний CPM по рынку)",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(Command("update_stats"), IsAdmin())
async def cmd_update_stats(message: Message, bot: Bot):
    """Команда для обновления статистики"""
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Использование: /update\\_stats <ID канала>", parse_mode=ParseMode.MARKDOWN)
        return
    
    try:
        channel_id = int(args[1])
    except:
        await message.answer("❌ Неверный ID")
        return
    
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.id == channel_id))
        channel = result.scalar_one_or_none()
    
    if not channel:
        await message.answer("❌ Канал не найден")
        return
    
    msg = await message.answer("⏳ Загружаю статистику...")
    
    # Имитируем callback для переиспользования логики
    class FakeCallback:
        data = f"update_stats:{channel_id}"
        
        def __init__(self, msg):
            self.message = msg
        
        async def answer(self, text, show_alert=False):
            pass  # Игнорируем answer для команды
    
    await cb_update_stats(FakeCallback(msg), bot)

# --- Управление CPM тематик ---
@router.message(Command("cpm"), IsAdmin())
async def cmd_cpm_list(message: Message):
    """Показать все тематики с CPM"""
    categories = await get_all_categories_cpm()
    
    # Сортируем по CPM
    sorted_cats = sorted(categories.items(), key=lambda x: x[1]["cpm"], reverse=True)
    
    text = "📊 **CPM по тематикам**\n"
    text += "_(источник: @CPM\\_TG)_\n\n"
    
    # Группируем по ценовым категориям
    expensive = [(k, v) for k, v in sorted_cats if v["cpm"] >= 800]
    medium = [(k, v) for k, v in sorted_cats if 400 <= v["cpm"] < 800]
    cheap = [(k, v) for k, v in sorted_cats if v["cpm"] < 400]
    
    if expensive:
        text += "💎 **Дорогие (от 800₽):**\n"
        for key, data in expensive:
            text += f"  `{key}`: {data['name']} — **{data['cpm']}₽**\n"
        text += "\n"
    
    if medium:
        text += "📊 **Средние (400-800₽):**\n"
        for key, data in medium:
            text += f"  `{key}`: {data['name']} — **{data['cpm']}₽**\n"
        text += "\n"
    
    if cheap:
        text += "💵 **Дешёвые (до 400₽):**\n"
        for key, data in cheap:
            text += f"  `{key}`: {data['name']} — **{data['cpm']}₽**\n"
    
    text += "\n**Редактировать:**\n"
    text += "`/set_cpm psychology 1500` — изменить CPM\n"
    text += "`/add_cpm newcat Название 600` — добавить тематику"
    
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("set_cpm"), IsAdmin())
async def cmd_set_cpm(message: Message):
    """Изменить CPM для тематики: /set_cpm psychology 1500"""
    args = message.text.split()
    
    if len(args) < 3:
        await message.answer(
            "**Использование:**\n"
            "`/set_cpm <ключ> <cpm>`\n\n"
            "**Пример:**\n"
            "`/set_cpm psychology 1500`\n\n"
            "Список тематик: /cpm",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    category_key = args[1].lower()
    try:
        new_cpm = int(args[2])
    except:
        await message.answer("❌ CPM должен быть числом")
        return
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(CategoryCPM).where(CategoryCPM.category_key == category_key)
        )
        cpm_entry = result.scalar_one_or_none()
        
        if not cpm_entry:
            await message.answer(f"❌ Тематика `{category_key}` не найдена\n\nСписок: /cpm", parse_mode=ParseMode.MARKDOWN)
            return
        
        old_cpm = cpm_entry.cpm
        cpm_entry.cpm = new_cpm
        cpm_entry.updated_at = datetime.utcnow()
        cpm_entry.updated_by = message.from_user.id
        await session.commit()
    
    await message.answer(
        f"✅ **CPM обновлён!**\n\n"
        f"{cpm_entry.name}\n"
        f"Было: {old_cpm}₽ → Стало: **{new_cpm}₽**",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(Command("add_cpm"), IsAdmin())
async def cmd_add_cpm(message: Message):
    """Добавить новую тематику: /add_cpm newcat 🆕 Название 600"""
    args = message.text.split(maxsplit=3)
    
    if len(args) < 4:
        await message.answer(
            "**Использование:**\n"
            "`/add_cpm <ключ> <название> <cpm>`\n\n"
            "**Пример:**\n"
            "`/add_cpm travel ✈️ Путешествия 800`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    category_key = args[1].lower()
    
    # Парсим название и CPM
    rest = args[3] if len(args) > 3 else args[2]
    parts = rest.rsplit(" ", 1)
    
    if len(parts) < 2:
        await message.answer("❌ Укажите название и CPM")
        return
    
    name = parts[0]
    try:
        cpm = int(parts[1])
    except:
        await message.answer("❌ CPM должен быть числом")
        return
    
    async with async_session_maker() as session:
        # Проверяем существование
        result = await session.execute(
            select(CategoryCPM).where(CategoryCPM.category_key == category_key)
        )
        if result.scalar_one_or_none():
            await message.answer(f"❌ Тематика `{category_key}` уже существует", parse_mode=ParseMode.MARKDOWN)
            return
        
        new_cat = CategoryCPM(
            category_key=category_key,
            name=name,
            cpm=cpm,
            updated_by=message.from_user.id
        )
        session.add(new_cat)
        await session.commit()
    
    await message.answer(
        f"✅ **Тематика добавлена!**\n\n"
        f"`{category_key}`: {name} — **{cpm}₽**",
        parse_mode=ParseMode.MARKDOWN
    )

@router.message(Command("del_cpm"), IsAdmin())
async def cmd_del_cpm(message: Message):
    """Удалить тематику: /del_cpm category_key"""
    args = message.text.split()
    
    if len(args) < 2:
        await message.answer("Использование: `/del_cpm <ключ>`", parse_mode=ParseMode.MARKDOWN)
        return
    
    category_key = args[1].lower()
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(CategoryCPM).where(CategoryCPM.category_key == category_key)
        )
        cpm_entry = result.scalar_one_or_none()
        
        if not cpm_entry:
            await message.answer(f"❌ Тематика `{category_key}` не найдена", parse_mode=ParseMode.MARKDOWN)
            return
        
        name = cpm_entry.name
        await session.delete(cpm_entry)
        await session.commit()
    
    await message.answer(f"✅ Тематика **{name}** удалена", parse_mode=ParseMode.MARKDOWN)

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
            
            # Начисляем комиссию менеджеру
            if order.manager_id:
                manager = await session.get(Manager, order.manager_id)
                if manager:
                    # Вычисляем комиссию
                    commission = order.final_price * (manager.commission_rate / 100)
                    
                    # Обновляем статистику менеджера
                    manager.balance += commission
                    manager.total_earned += commission
                    manager.total_sales += 1
                    manager.total_revenue += order.final_price
                    manager.last_active = datetime.utcnow()
                    
                    # Проверяем, новый ли это клиент для менеджера
                    # (упрощённо — считаем всех клиентов)
                    manager.clients_count += 1
                    
                    # Начисляем XP за продажу
                    xp_earned = 50 + int(float(order.final_price) / 100)  # 50 XP + 1 XP за каждые 100₽
                    await add_manager_xp(manager.id, xp_earned, session)
                    
                    # Проверяем достижения
                    new_achievements = await check_achievements(manager.id, session)
                    
                    # Уведомляем менеджера
                    try:
                        achievement_text = ""
                        if new_achievements:
                            for ach in new_achievements:
                                ach_info = ACHIEVEMENTS.get(ach, {})
                                achievement_text += f"\n🏆 {ach_info.get('emoji', '')} {ach_info.get('name', ach)}"
                        
                        await bot.send_message(
                            manager.telegram_id,
                            f"💰 **Комиссия начислена!**\n\n"
                            f"Заказ #{order_id}\n"
                            f"Сумма заказа: {order.final_price:,.0f}₽\n"
                            f"Ваша комиссия: **{commission:,.0f}₽** ({manager.commission_rate}%)\n"
                            f"+{xp_earned} XP"
                            f"{achievement_text}",
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

# --- Аналитика (кнопка) ---
@router.message(F.text == "📊 Аналитика", IsAdmin())
async def admin_analytics_button(message: Message, state: FSMContext):
    """Обработчик кнопки Аналитика — вызывает команду /analytics"""
    # Показываем список каналов с аналитикой
    async with async_session_maker() as session:
        result = await session.execute(select(Channel).where(Channel.is_active == True))
        channels = result.scalars().all()
    
    if not channels:
        await message.answer("❌ Нет каналов")
        return
    
    text = "📊 **Аналитика каналов**\n\n"
    for ch in channels:
        subs = f"{ch.subscribers:,}" if ch.subscribers else "—"
        reach = f"{ch.avg_reach:,}" if ch.avg_reach else "—"
        err = f"{float(ch.err_percent):.1f}%" if ch.err_percent else "—"
        cat = CHANNEL_CATEGORIES.get(ch.category, {}).get("name", "—") if ch.category else "—"
        text += f"• **{ch.name}** (ID: {ch.id})\n"
        text += f"  👥 {subs} | 👁 {reach} | ERR: {err} | 🏷 {cat}\n\n"
    
    text += "**Команды:**\n"
    text += "/analytics <ID> — детали канала\n"
    text += "/update\\_stats <ID> — обновить через TGStat\n"
    text += "/set\\_category <ID> — установить тематику\n"
    text += "/manual\\_stats <ID> — ввести данные вручную"
    
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# --- Статистика ---
@router.message(F.text == "📈 Статистика", IsAdmin())
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

# ==================== СИСТЕМА МЕНЕДЖЕРОВ ====================

# --- Команда /manager - вход в панель менеджера ---
@router.message(Command("manager"))
async def manager_panel(message: Message, state: FSMContext):
    await state.clear()
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if not manager:
        # Предлагаем регистрацию
        await message.answer(
            "👋 **Добро пожаловать в программу менеджеров!**\n\n"
            "Станьте частью нашей команды и зарабатывайте на продаже рекламы.\n\n"
            "**Что вы получите:**\n"
            "💰 Комиссия 10-25% от каждой продажи\n"
            "📚 Бесплатное обучение\n"
            "🎯 Бонусы за выполнение заданий\n"
            "🏆 Система достижений и уровней\n\n"
            "Хотите присоединиться?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, хочу стать менеджером", callback_data="manager_register")],
                [InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="cancel")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Показываем панель менеджера
    level_info = await get_manager_level(manager)
    
    await message.answer(
        f"👤 **Панель менеджера**\n\n"
        f"{level_info['emoji']} Уровень {manager.level}: {level_info['name']}\n"
        f"📊 XP: {manager.experience_points:,}\n"
        f"💰 Баланс: {manager.balance:,.0f}₽\n"
        f"📈 Продаж: {manager.total_sales}\n\n"
        f"Выберите действие:",
        reply_markup=get_manager_menu(),
        parse_mode=ParseMode.MARKDOWN
    )

# --- Регистрация менеджера ---
@router.callback_query(F.data == "manager_register")
async def start_manager_registration(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    await callback.message.edit_text(
        "📝 **Регистрация менеджера**\n\n"
        "Шаг 1/2: Введите ваш номер телефона:\n"
        "(формат: +7XXXXXXXXXX)",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerStates.registration_phone)

@router.message(ManagerStates.registration_phone)
async def receive_manager_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    
    # Простая валидация
    if not phone.startswith("+") or len(phone) < 10:
        await message.answer("❌ Введите корректный номер телефона (+7XXXXXXXXXX)")
        return
    
    await state.update_data(phone=phone)
    
    await message.answer(
        f"📱 Телефон: {phone}\n\n"
        f"Подтверждаете регистрацию?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_manager_reg")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ])
    )
    await state.set_state(ManagerStates.registration_confirm)

@router.callback_query(F.data == "confirm_manager_reg", ManagerStates.registration_confirm)
async def confirm_manager_registration(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    
    async with async_session_maker() as session:
        manager = Manager(
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
            phone=data.get("phone")
        )
        session.add(manager)
        await session.commit()
    
    await state.clear()
    
    await callback.message.edit_text(
        "🎉 **Добро пожаловать в команду!**\n\n"
        "Вы успешно зарегистрированы как менеджер.\n\n"
        "**Следующий шаг:** пройдите обучение чтобы начать работу.\n\n"
        "Нажмите /manager чтобы открыть панель.",
        parse_mode=ParseMode.MARKDOWN
    )

# --- Профиль менеджера ---
@router.message(F.text == "📊 Мой профиль", IsManager())
async def manager_profile(message: Message):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if not manager:
        await message.answer("❌ Вы не зарегистрированы. Нажмите /manager")
        return
    
    level_info = await get_manager_level(manager)
    next_level = MANAGER_LEVELS.get(manager.level + 1)
    
    # Прогресс до следующего уровня
    if next_level:
        current_xp = manager.experience_points
        next_xp = next_level["min_xp"]
        prev_xp = level_info["min_xp"]
        progress = int((current_xp - prev_xp) / (next_xp - prev_xp) * 10)
        progress_bar = "▓" * progress + "░" * (10 - progress)
        next_level_text = f"\n📈 До уровня {manager.level + 1}: {progress_bar} {current_xp}/{next_xp}"
    else:
        next_level_text = "\n🏆 Максимальный уровень достигнут!"
    
    status_names = {
        "trainee": "🌱 Стажёр (обучение)",
        "active": "✅ Активный",
        "senior": "⭐ Старший",
        "lead": "👑 Лид"
    }
    
    await message.answer(
        f"👤 **Ваш профиль**\n\n"
        f"📛 {manager.first_name or 'Менеджер'}\n"
        f"📱 {manager.phone or 'Не указан'}\n\n"
        f"**Уровень и статус:**\n"
        f"{level_info['emoji']} Уровень {manager.level}: {level_info['name']}\n"
        f"📊 XP: {manager.experience_points:,}\n"
        f"{status_names.get(manager.status, manager.status)}"
        f"{next_level_text}\n\n"
        f"**Комиссия:** {manager.commission_rate}% от продаж\n\n"
        f"**Статистика:**\n"
        f"💰 Всего заработано: {manager.total_earned:,.0f}₽\n"
        f"📦 Продаж: {manager.total_sales}\n"
        f"👥 Клиентов: {manager.clients_count}\n"
        f"💵 Оборот: {manager.total_revenue:,.0f}₽",
        parse_mode=ParseMode.MARKDOWN
    )

# --- Баланс менеджера ---
@router.message(F.text == "💰 Баланс", IsManager())
async def manager_balance(message: Message):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if not manager:
        return
    
    await message.answer(
        f"💰 **Ваш баланс**\n\n"
        f"Доступно к выводу: **{manager.balance:,.0f}₽**\n\n"
        f"Минимальная сумма вывода: 500₽",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Вывести средства", callback_data="request_payout")],
            [InlineKeyboardButton(text="📜 История выплат", callback_data="payout_history")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )

# --- Запрос на вывод ---
@router.callback_query(F.data == "request_payout")
async def request_payout(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == callback.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if not manager or float(manager.balance) < 500:
        await callback.message.edit_text("❌ Минимальная сумма вывода: 500₽")
        return
    
    await callback.message.edit_text(
        f"💸 **Вывод средств**\n\n"
        f"Доступно: {manager.balance:,.0f}₽\n\n"
        f"Введите сумму для вывода:",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerStates.payout_amount)

@router.message(ManagerStates.payout_amount)
async def receive_payout_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except:
        await message.answer("❌ Введите число")
        return
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if amount < 500:
        await message.answer("❌ Минимальная сумма: 500₽")
        return
    
    if amount > float(manager.balance):
        await message.answer(f"❌ Недостаточно средств. Доступно: {manager.balance:,.0f}₽")
        return
    
    await state.update_data(payout_amount=amount)
    
    await message.answer(
        f"💸 Сумма: {amount:,}₽\n\n"
        f"Выберите способ получения:",
        reply_markup=get_payout_keyboard()
    )
    await state.set_state(ManagerStates.payout_method)

@router.callback_query(F.data.startswith("payout:"), ManagerStates.payout_method)
async def select_payout_method(callback: CallbackQuery, state: FSMContext):
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
    data = await state.get_data()
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
        
        # Создаём заявку
        payout = ManagerPayout(
            manager_id=manager.id,
            amount=Decimal(str(data["payout_amount"])),
            payment_method=data["payout_method"],
            payment_details=message.text.strip()
        )
        session.add(payout)
        
        # Списываем с баланса
        manager.balance -= Decimal(str(data["payout_amount"]))
        await session.commit()
    
    await state.clear()
    
    await message.answer(
        f"✅ **Заявка на вывод создана!**\n\n"
        f"💸 Сумма: {data['payout_amount']:,}₽\n"
        f"📱 Способ: {data['payout_method']}\n\n"
        f"Выплата будет обработана в течение 24 часов.",
        reply_markup=get_manager_menu(),
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Уведомляем админа
    for admin_id in ADMIN_IDS:
        try:
            bot = message.bot
            await bot.send_message(
                admin_id,
                f"💸 **Новая заявка на вывод!**\n\n"
                f"👤 {manager.first_name} (@{manager.username})\n"
                f"💰 {data['payout_amount']:,}₽\n"
                f"📱 {data['payout_method']}: {message.text}",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass

# --- Обучение ---
@router.message(F.text == "📚 Обучение", IsManager())
async def manager_training(message: Message):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    if manager.training_completed:
        await message.answer(
            "🎓 **Обучение пройдено!**\n\n"
            f"Ваш результат: {manager.training_score} баллов\n\n"
            "Вы можете пересмотреть уроки:",
            reply_markup=get_training_keyboard(1, len(DEFAULT_LESSONS)),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        lesson = DEFAULT_LESSONS[manager.current_lesson - 1] if manager.current_lesson <= len(DEFAULT_LESSONS) else None
        
        await message.answer(
            f"📚 **Обучение**\n\n"
            f"Текущий урок: {manager.current_lesson}/{len(DEFAULT_LESSONS)}\n"
            f"{'✅' if manager.training_completed else '📖'} {lesson['title'] if lesson else 'Завершено'}\n\n"
            f"Пройдите все уроки чтобы начать работу!",
            reply_markup=get_training_keyboard(manager.current_lesson, len(DEFAULT_LESSONS)),
            parse_mode=ParseMode.MARKDOWN
        )

@router.callback_query(F.data.startswith("lesson:"))
async def view_lesson(callback: CallbackQuery, state: FSMContext):
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
    await callback.answer()
    lesson_num = int(callback.data.split(":")[1])
    lesson = DEFAULT_LESSONS[lesson_num - 1]
    
    await state.update_data(
        quiz_lesson=lesson_num,
        quiz_questions=lesson["quiz_questions"],
        quiz_index=0,
        quiz_correct=0
    )
    
    # Показываем первый вопрос
    q = lesson["quiz_questions"][0]
    await callback.message.edit_text(
        f"📝 **Тест по уроку {lesson_num}**\n\n"
        f"Вопрос 1/{len(lesson['quiz_questions'])}:\n\n"
        f"{q['q']}",
        reply_markup=get_quiz_keyboard(q["options"], 0),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.set_state(ManagerStates.taking_quiz)

@router.callback_query(F.data.startswith("quiz_answer:"), ManagerStates.taking_quiz)
async def process_quiz_answer(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split(":")
    q_index = int(parts[1])
    answer = int(parts[2])
    
    data = await state.get_data()
    questions = data["quiz_questions"]
    correct = data["quiz_correct"]
    
    # Проверяем ответ
    if questions[q_index]["correct"] == answer:
        correct += 1
    
    await state.update_data(quiz_correct=correct)
    
    # Следующий вопрос или результат
    next_index = q_index + 1
    if next_index < len(questions):
        q = questions[next_index]
        await callback.message.edit_text(
            f"📝 **Тест**\n\n"
            f"Вопрос {next_index + 1}/{len(questions)}:\n\n"
            f"{q['q']}",
            reply_markup=get_quiz_keyboard(q["options"], next_index),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        # Тест завершён
        score = int(correct / len(questions) * 100)
        passed = score >= 70
        lesson_num = data["quiz_lesson"]
        lesson = DEFAULT_LESSONS[lesson_num - 1]
        
        async with async_session_maker() as session:
            result = await session.execute(
                select(Manager).where(Manager.telegram_id == callback.from_user.id)
            )
            manager = result.scalar_one_or_none()
            
            if passed:
                # Начисляем XP
                manager.experience_points += lesson["reward_points"]
                manager.training_score += score
                
                # Переходим к следующему уроку
                if manager.current_lesson == lesson_num:
                    manager.current_lesson += 1
                
                # Проверяем завершение обучения
                if manager.current_lesson > len(DEFAULT_LESSONS):
                    manager.training_completed = True
                    manager.status = "active"
                
                await session.commit()
                
                # Проверяем достижения
                new_achievements = await check_achievements(manager.id, session)
        
        if passed:
            next_text = ""
            if lesson_num < len(DEFAULT_LESSONS):
                next_text = f"\n\n➡️ Переходите к уроку {lesson_num + 1}!"
            else:
                next_text = "\n\n🎓 Поздравляем! Обучение завершено!"
            
            await callback.message.edit_text(
                f"✅ **Тест пройден!**\n\n"
                f"Результат: {score}%\n"
                f"Правильных ответов: {correct}/{len(questions)}\n\n"
                f"+{lesson['reward_points']} XP"
                f"{next_text}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📚 К обучению", callback_data="back_to_training")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await callback.message.edit_text(
                f"❌ **Тест не пройден**\n\n"
                f"Результат: {score}% (нужно 70%)\n"
                f"Правильных ответов: {correct}/{len(questions)}\n\n"
                f"Пересмотрите урок и попробуйте снова.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📖 Пересмотреть урок", callback_data=f"lesson:{lesson_num}")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_training")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
        
        await state.clear()

@router.callback_query(F.data == "back_to_training")
async def back_to_training(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == callback.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    await callback.message.edit_text(
        f"📚 **Обучение**\n\n"
        f"Прогресс: {manager.current_lesson - 1}/{len(DEFAULT_LESSONS)} уроков\n"
        f"{'🎓 Обучение завершено!' if manager.training_completed else ''}",
        reply_markup=get_training_keyboard(manager.current_lesson, len(DEFAULT_LESSONS)),
        parse_mode=ParseMode.MARKDOWN
    )

# --- Достижения ---
@router.message(F.text == "🏆 Достижения", IsManager())
async def manager_achievements(message: Message):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
        
        # Получаем достижения
        ach_result = await session.execute(
            select(ManagerAchievement).where(ManagerAchievement.manager_id == manager.id)
        )
        earned_achievements = {a.achievement_type for a in ach_result.scalars().all()}
    
    text = "🏆 **Ваши достижения**\n\n"
    
    for ach_type, ach_info in ACHIEVEMENTS.items():
        if ach_type in earned_achievements:
            text += f"✅ {ach_info['emoji']} **{ach_info['name']}** (+{ach_info['xp']} XP)\n"
        else:
            text += f"🔒 {ach_info['emoji']} {ach_info['name']}\n   _{ach_info['description']}_\n"
    
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# --- Реферальная ссылка ---
@router.message(F.text == "🔗 Моя ссылка", IsManager())
async def manager_ref_link(message: Message):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Manager).where(Manager.telegram_id == message.from_user.id)
        )
        manager = result.scalar_one_or_none()
    
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{manager.id}"
    
    await message.answer(
        f"🔗 **Ваша реферальная ссылка:**\n\n"
        f"`{ref_link}`\n\n"
        f"Отправляйте эту ссылку клиентам.\n"
        f"Все их заказы будут закреплены за вами,\n"
        f"и вы получите комиссию {manager.commission_rate}%!",
        parse_mode=ParseMode.MARKDOWN
    )

# --- Кнопка назад для менеджера ---
@router.callback_query(F.data == "manager_back")
async def manager_back(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.delete()

# ==================== ЗАПУСК ====================

async def on_startup(bot: Bot):
    await init_db()
    await migrate_db()  # Добавляем новые колонки если их нет
    await init_category_cpm()  # Инициализируем CPM тематик
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

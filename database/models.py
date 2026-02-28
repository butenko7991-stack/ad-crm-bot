"""
Модели базы данных
"""
from datetime import datetime, date, time
from decimal import Decimal
from typing import Optional, List

from sqlalchemy import (
    Column, Integer, String, BigInteger, Boolean, DateTime, Date, Time,
    Numeric, Text, ForeignKey, JSON
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Channel(Base):
    """Каналы для размещения рекламы"""
    __tablename__ = "channels"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    name = Column(String(255), nullable=False)
    username = Column(String(255))
    description = Column(Text)
    category = Column(String(100))
    
    # Цены по форматам
    prices = Column(JSON, default={"1/24": 0, "1/48": 0, "2/48": 0, "native": 0})
    
    # Аналитика
    subscribers = Column(Integer, default=0)
    avg_reach = Column(Integer, default=0)
    avg_reach_24h = Column(Integer, default=0)
    avg_reach_48h = Column(Integer, default=0)
    avg_reach_72h = Column(Integer, default=0)
    err_percent = Column(Numeric(5, 2), default=0)
    err24_percent = Column(Numeric(5, 2), default=0)
    ci_index = Column(Numeric(8, 2), default=0)
    cpm = Column(Numeric(10, 2), default=0)
    telemetr_id = Column(String(20))
    analytics_updated = Column(DateTime)
    
    # Старые поля для совместимости
    price_morning = Column(Numeric(12, 2), default=0)
    price_evening = Column(Numeric(12, 2), default=0)
    
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    slots = relationship("Slot", back_populates="channel", cascade="all, delete-orphan")


class CategoryCPM(Base):
    """CPM по тематикам (редактируется через бота)"""
    __tablename__ = "category_cpm"
    
    id = Column(Integer, primary_key=True)
    category_key = Column(String(50), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    cpm = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(BigInteger)


class Slot(Base):
    """Слоты для размещения"""
    __tablename__ = "slots"
    
    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    slot_date = Column(Date, nullable=False)
    slot_time = Column(Time, nullable=False)
    status = Column(String(20), default="available")
    reserved_until = Column(DateTime)
    reserved_by = Column(BigInteger)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    channel = relationship("Channel", back_populates="slots")
    order = relationship("Order", back_populates="slot", uselist=False)


class Client(Base):
    """Клиенты (рекламодатели)"""
    __tablename__ = "clients"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    total_orders = Column(Integer, default=0)
    total_spent = Column(Numeric(12, 2), default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    orders = relationship("Order", back_populates="client")


class Manager(Base):
    """Менеджеры по продажам"""
    __tablename__ = "managers"
    
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255))
    first_name = Column(String(255))
    
    # Статус и уровень
    status = Column(String(20), default="trainee")
    level = Column(Integer, default=1)
    experience_points = Column(Integer, default=0)
    
    # Финансы
    balance = Column(Numeric(12, 2), default=0)
    total_earned = Column(Numeric(12, 2), default=0)
    commission_rate = Column(Numeric(5, 2), default=10)
    
    # Статистика
    total_sales = Column(Integer, default=0)
    total_revenue = Column(Numeric(12, 2), default=0)
    
    # Обучение
    training_completed = Column(Boolean, default=False)
    training_score = Column(Integer, default=0)
    current_lesson = Column(Integer, default=1)
    
    hired_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    
    orders = relationship("Order", back_populates="manager")
    payouts = relationship("ManagerPayout", back_populates="manager")


class Order(Base):
    """Заказы на размещение"""
    __tablename__ = "orders"
    
    id = Column(Integer, primary_key=True)
    slot_id = Column(Integer, ForeignKey("slots.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    manager_id = Column(Integer, ForeignKey("managers.id"))
    
    format_type = Column(String(20), default="1/24")
    base_price = Column(Numeric(12, 2), default=0)
    discount_percent = Column(Numeric(5, 2), default=0)
    final_price = Column(Numeric(12, 2), default=0)
    
    status = Column(String(30), default="pending")
    payment_method = Column(String(50))
    payment_screenshot = Column(String(500))
    
    ad_content = Column(Text)
    ad_file_id = Column(String(500))
    ad_file_type = Column(String(20))
    
    created_at = Column(DateTime, default=datetime.utcnow)
    paid_at = Column(DateTime)
    posted_at = Column(DateTime)
    
    slot = relationship("Slot", back_populates="order")
    client = relationship("Client", back_populates="orders")
    manager = relationship("Manager", back_populates="orders")


class ManagerPayout(Base):
    """Выплаты менеджерам"""
    __tablename__ = "manager_payouts"
    
    id = Column(Integer, primary_key=True)
    manager_id = Column(Integer, ForeignKey("managers.id"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    method = Column(String(50))
    details = Column(String(500))
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)
    processed_at = Column(DateTime)
    processed_by = Column(BigInteger)
    
    manager = relationship("Manager", back_populates="payouts")


class ScheduledPost(Base):
    """Запланированные посты (автопостинг)"""
    __tablename__ = "scheduled_posts"
    
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"))
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    
    content = Column(Text)
    file_id = Column(String(500))
    file_type = Column(String(20))
    
    scheduled_time = Column(DateTime, nullable=False)
    delete_after_hours = Column(Integer, default=24)
    
    status = Column(String(20), default="pending")
    message_id = Column(BigInteger)
    posted_at = Column(DateTime)
    deleted_at = Column(DateTime)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(BigInteger)


class Competition(Base):
    """Соревнования менеджеров"""
    __tablename__ = "competitions"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    prize_pool = Column(Numeric(12, 2), default=0)
    metric = Column(String(50), default="sales")
    status = Column(String(20), default="active")
    created_at = Column(DateTime, default=datetime.utcnow)


class AIInsight(Base):
    """Инсайты для самообучения AI-тренера"""
    __tablename__ = "ai_insights"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    topic = Column(String(100))
    question = Column(Text)
    answer = Column(Text)
    feedback = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow)

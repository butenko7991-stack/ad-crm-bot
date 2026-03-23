"""
Подключение к базе данных
"""
import logging
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from config import DATABASE_URL


logger = logging.getLogger(__name__)


# Исправляем URL для asyncpg
db_url = DATABASE_URL
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

# Создаём движок
engine = create_async_engine(db_url, echo=False)

# Создаём фабрику сессий
async_session_maker = async_sessionmaker(
    engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)


async def init_db():
    """Инициализация базы данных"""
    from database.models import Base
    from sqlalchemy import text

    # Создаём таблицы в отдельной транзакции, чтобы они были зафиксированы
    # независимо от результата миграций ниже.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Добавляем новые колонки в существующие таблицы (миграция).
    # Каждая миграция выполняется в отдельной транзакции: если одна из них
    # завершается ошибкой (например, UPDATE с несуществующим столбцом), это
    # не откатывает остальные миграции и не затрагивает уже созданные таблицы.
    migrations = [
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS avg_reach_24h INTEGER DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS avg_reach_48h INTEGER DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS avg_reach_72h INTEGER DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS err_percent NUMERIC(5, 2) DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS err24_percent NUMERIC(5, 2) DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS ci_index NUMERIC(8, 2) DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS cpm NUMERIC(10, 2) DEFAULT 0",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS telemetr_id VARCHAR(20)",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS analytics_updated TIMESTAMP",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ad_content TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ad_file_id VARCHAR(500)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS ad_file_type VARCHAR(20)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS format_type VARCHAR(20) DEFAULT '1/24'",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_screenshot VARCHAR(500)",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS posted_at TIMESTAMP",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS promo_code VARCHAR(50)",
        "ALTER TABLE managers ADD COLUMN IF NOT EXISTS experience_points INTEGER DEFAULT 0",
        "ALTER TABLE managers ADD COLUMN IF NOT EXISTS total_earned NUMERIC(12, 2) DEFAULT 0",
        "ALTER TABLE managers ADD COLUMN IF NOT EXISTS training_score INTEGER DEFAULT 0",
        "ALTER TABLE managers ADD COLUMN IF NOT EXISTS current_lesson INTEGER DEFAULT 1",
        "ALTER TABLE clients ADD COLUMN IF NOT EXISTS referrer_id INTEGER REFERENCES managers(id)",
        "ALTER TABLE managers ADD COLUMN IF NOT EXISTS max_id BIGINT",
        "ALTER TABLE clients ADD COLUMN IF NOT EXISTS max_id BIGINT",
        "ALTER TABLE scheduled_posts ALTER COLUMN order_id DROP NOT NULL",
        "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS scheduled_time TIMESTAMP",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='scheduled_posts' AND column_name='scheduled_at') THEN UPDATE scheduled_posts SET scheduled_time = scheduled_at WHERE scheduled_time IS NULL AND scheduled_at IS NOT NULL; END IF; END $$",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='scheduled_posts' AND column_name='scheduled_at') THEN ALTER TABLE scheduled_posts ALTER COLUMN scheduled_at DROP NOT NULL; END IF; END $$",
        # Миграции для scheduled_posts (добавлены позже)
        "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS inline_buttons TEXT",
        "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS delete_after_hours INTEGER DEFAULT 24",
        "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS message_id BIGINT",
        "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP",
        "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS created_by BIGINT",
        "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS payment_screenshot VARCHAR(500)",
        # Миграции для post_analytics (добавлены позже)
        "ALTER TABLE post_analytics ADD COLUMN IF NOT EXISTS channel_id INTEGER REFERENCES channels(id) ON DELETE CASCADE",
        "ALTER TABLE post_analytics ADD COLUMN IF NOT EXISTS saves INTEGER DEFAULT 0",
        "ALTER TABLE post_analytics ADD COLUMN IF NOT EXISTS comments INTEGER DEFAULT 0",
        "ALTER TABLE post_analytics ADD COLUMN IF NOT EXISTS ai_recommendation TEXT",
        "ALTER TABLE post_analytics ADD COLUMN IF NOT EXISTS recorded_by BIGINT",
        "ALTER TABLE post_analytics ADD COLUMN IF NOT EXISTS order_id INTEGER REFERENCES orders(id) ON DELETE SET NULL",
        "ALTER TABLE managers ADD COLUMN IF NOT EXISTS timezone_offset INTEGER DEFAULT 3",
        "ALTER TABLE channels ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
        "UPDATE channels SET is_active = TRUE WHERE is_active IS NULL",
        "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS signature VARCHAR(255)",
        # Заполняем channel_id для записей post_analytics, у которых он не задан,
        # беря значение из связанного scheduled_post
        "UPDATE post_analytics pa SET channel_id = sp.channel_id FROM scheduled_posts sp WHERE pa.scheduled_post_id = sp.id AND pa.channel_id IS NULL",
        # Индексы для ускорения частых запросов (планировщик, бронирование, статистика)
        "CREATE INDEX IF NOT EXISTS idx_scheduled_posts_status_time ON scheduled_posts(status, scheduled_time)",
        "CREATE INDEX IF NOT EXISTS idx_scheduled_posts_status_posted ON scheduled_posts(status, posted_at, deleted_at, message_id)",
        "CREATE INDEX IF NOT EXISTS idx_slots_channel_status_date ON slots(channel_id, status, slot_date)",
        "CREATE INDEX IF NOT EXISTS idx_orders_client_id ON orders(client_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_manager_id ON orders(manager_id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_channel_status ON orders(slot_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_managers_telegram_id ON managers(telegram_id)",
        "CREATE INDEX IF NOT EXISTS idx_clients_telegram_id ON clients(telegram_id)",
        "CREATE INDEX IF NOT EXISTS idx_promo_codes_code ON promo_codes(code) WHERE is_active = TRUE",
        "CREATE INDEX IF NOT EXISTS idx_post_analytics_channel ON post_analytics(channel_id)",
        "CREATE INDEX IF NOT EXISTS idx_post_analytics_order ON post_analytics(order_id)",
    ]

    for migration in migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(migration))
        except Exception as e:
            logger.warning(f"Migration skipped ({migration!r}): {e}")


async def get_session() -> AsyncSession:
    """Получить сессию БД"""
    async with async_session_maker() as session:
        yield session

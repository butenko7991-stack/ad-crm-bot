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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Добавляем новые колонки в существующие таблицы (миграция)
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
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method VARCHAR(50)",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_screenshot VARCHAR(500)",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP",
            "ALTER TABLE orders ADD COLUMN IF NOT EXISTS posted_at TIMESTAMP",
            "ALTER TABLE managers ADD COLUMN IF NOT EXISTS experience_points INTEGER DEFAULT 0",
            "ALTER TABLE managers ADD COLUMN IF NOT EXISTS total_earned NUMERIC(12, 2) DEFAULT 0",
            "ALTER TABLE managers ADD COLUMN IF NOT EXISTS training_score INTEGER DEFAULT 0",
            "ALTER TABLE managers ADD COLUMN IF NOT EXISTS current_lesson INTEGER DEFAULT 1",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS referrer_id INTEGER REFERENCES managers(id)",
            "ALTER TABLE managers ADD COLUMN IF NOT EXISTS max_id BIGINT",
            "ALTER TABLE clients ADD COLUMN IF NOT EXISTS max_id BIGINT",
            "ALTER TABLE scheduled_posts ALTER COLUMN order_id DROP NOT NULL",
            "ALTER TABLE scheduled_posts ADD COLUMN IF NOT EXISTS scheduled_time TIMESTAMP",
            "UPDATE scheduled_posts SET scheduled_time = scheduled_at WHERE scheduled_time IS NULL AND scheduled_at IS NOT NULL",
            "ALTER TABLE scheduled_posts ALTER COLUMN scheduled_at DROP NOT NULL",
        ]

        for migration in migrations:
            try:
                await conn.execute(text(migration))
            except Exception as e:
                logger.warning(f"Migration skipped ({migration!r}): {e}")


async def get_session() -> AsyncSession:
    """Получить сессию БД"""
    async with async_session_maker() as session:
        yield session

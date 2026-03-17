"""
Журнал улучшений бота (система самосовершенствования).

Сохраняет:
  - AI-рекомендации, полученные из Claude по бизнес-метрикам
  - Ручные записи об исправлениях и доработках, одобренных администратором

Журнал хранится в файле IMPROVEMENT_LOG_PATH (по умолчанию improvement_log.json)
и не включается в репозиторий (.gitignore).

Использование:
    from services.improvement_log import log_improvement, get_recent_improvements

    # Сохранить AI-рекомендацию
    await log_improvement("AI-анализ метрик", ai_text, category="ai_suggestion")

    # Сохранить ручное улучшение
    await log_improvement("Слот освобождён при отмене", "Исправлен обработчик...", category="bugfix")
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Путь к файлу журнала (рантайм, не в репозитории)
IMPROVEMENT_LOG_PATH: Path = Path(os.getenv("IMPROVEMENT_LOG_PATH", "improvement_log.json"))

# Максимальное количество записей в журнале
IMPROVEMENT_LOG_MAX_ENTRIES = 200

# Категории улучшений
CATEGORIES = {
    "ai_suggestion": "💡 AI-рекомендация",
    "bugfix": "🐛 Исправление ошибки",
    "feature": "✨ Новая функция",
    "refactor": "🔧 Рефакторинг",
    "security": "🔐 Безопасность",
    "performance": "⚡ Производительность",
    "admin_note": "📝 Заметка администратора",
}


def log_improvement(
    title: str,
    description: str,
    category: str = "admin_note",
    author: str = "system",
) -> None:
    """Записать улучшение в журнал.

    Args:
        title: Краткое название улучшения.
        description: Подробное описание.
        category: Категория из CATEGORIES (ключ).
        author: Кто добавил ('system', 'ai', 'admin:<id>').
    """
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": category,
        "title": title[:200],
        "description": description[:2000],
        "author": author,
    }

    try:
        existing: list = []
        if IMPROVEMENT_LOG_PATH.exists():
            try:
                existing = json.loads(IMPROVEMENT_LOG_PATH.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except (json.JSONDecodeError, OSError):
                existing = []

        existing.append(entry)

        if len(existing) > IMPROVEMENT_LOG_MAX_ENTRIES:
            existing = existing[-IMPROVEMENT_LOG_MAX_ENTRIES:]

        IMPROVEMENT_LOG_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Не удалось записать улучшение в журнал: {e}")


def get_recent_improvements(limit: int = 10, category: Optional[str] = None) -> list[dict]:
    """Вернуть последние `limit` записей из журнала.

    Args:
        limit: Максимальное количество записей.
        category: Фильтр по категории (None = все).
    """
    try:
        if not IMPROVEMENT_LOG_PATH.exists():
            return []
        entries = json.loads(IMPROVEMENT_LOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return []

        if category:
            entries = [e for e in entries if e.get("category") == category]

        return entries[-limit:][::-1]  # Последние сверху
    except Exception as e:
        logger.warning(f"Не удалось прочитать журнал улучшений: {e}")
        return []


def format_improvement_entry(entry: dict, index: int = 0) -> str:
    """Форматировать запись журнала для вывода в Telegram."""
    cat_key = entry.get("category", "admin_note")
    cat_label = CATEGORIES.get(cat_key, cat_key)
    ts = entry.get("ts", "")[:16].replace("T", " ")
    title = entry.get("title", "")
    description = entry.get("description", "")
    author = entry.get("author", "")

    # Обрезаем описание для чата
    if len(description) > 400:
        description = description[:400] + "…"

    lines = [
        f"*{index + 1}. {title}*",
        f"{cat_label} | {ts} UTC | _{author}_",
        description,
    ]
    return "\n".join(lines)


def get_improvement_stats() -> dict:
    """Вернуть статистику по журналу улучшений."""
    try:
        if not IMPROVEMENT_LOG_PATH.exists():
            return {"total": 0, "by_category": {}}
        entries = json.loads(IMPROVEMENT_LOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return {"total": 0, "by_category": {}}

        by_cat: dict[str, int] = {}
        for e in entries:
            cat = e.get("category", "unknown")
            by_cat[cat] = by_cat.get(cat, 0) + 1

        return {"total": len(entries), "by_category": by_cat}
    except Exception as e:
        logger.warning(f"Не удалось прочитать статистику журнала: {e}")
        return {"total": 0, "by_category": {}}

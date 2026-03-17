"""
Библиотека ошибок бота.

Структура:
  - KNOWN_ERRORS — список известных шаблонов ошибок с описанием и способом решения.
  - lookup_error()   — поиск подходящего шаблона по типу исключения и тексту трейсбека.
  - record_unknown_error() — сохранение неизвестной ошибки в файл журнала для последующего
                             анализа и пополнения библиотеки.

Журнал неизвестных ошибок хранится в файле ERROR_LOG_PATH (по умолчанию error_log.json
рядом с кодом) и не включается в репозиторий (.gitignore).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Путь к файлу с неизвестными ошибками (рантайм, не в репозитории)
ERROR_LOG_PATH: Path = Path(os.getenv("ERROR_LOG_PATH", "error_log.json"))

# Максимальное количество записей в файле журнала (старые удаляются)
ERROR_LOG_MAX_ENTRIES = 500


# ──────────────────────────────────────────────────────────────────────────────
# Известные шаблоны ошибок
# Каждый шаблон — словарь:
#   id        — уникальный ключ
#   category  — категория (db, telegram, config, payment, slot, datetime, auth, ai)
#   match_type — список имён классов исключений (str), которые подходят. [] = любое.
#   patterns  — список строк/regex-подстрок, которые должны присутствовать в трейсбеке
#               или сообщении исключения (OR-логика: достаточно одного совпадения).
#   title     — краткое название
#   description — объяснение причины
#   solution  — шаги решения
# ──────────────────────────────────────────────────────────────────────────────
KNOWN_ERRORS: list[dict] = [
    # ── База данных ────────────────────────────────────────────────────────────
    {
        "id": "db_connection_refused",
        "category": "db",
        "match_type": [],
        "patterns": [
            "Connection refused",
            "could not connect to server",
            "asyncpg.exceptions.ConnectionFailureError",
            "sqlalchemy.exc.OperationalError",
        ],
        "title": "БД недоступна",
        "description": "PostgreSQL не отвечает или DATABASE_URL задан неверно.",
        "solution": (
            "1. Убедитесь, что сервер PostgreSQL запущен.\n"
            "2. Проверьте переменную окружения DATABASE_URL.\n"
            "3. Проверьте сетевую доступность (firewall, порт 5432).\n"
            "4. Запустите `adm_diagnostics` в панели администратора для проверки."
        ),
    },
    {
        "id": "db_unique_violation",
        "category": "db",
        "match_type": ["UniqueViolationError", "IntegrityError"],
        "patterns": [
            "UniqueViolationError",
            "duplicate key",
            "UNIQUE constraint failed",
        ],
        "title": "Дублирование записи в БД",
        "description": "Попытка вставить запись с уже существующим уникальным ключом.",
        "solution": (
            "1. Проверьте, не создаётся ли объект повторно (двойной клик на кнопку).\n"
            "2. Используйте `INSERT ... ON CONFLICT DO NOTHING` или проверку перед вставкой.\n"
            "3. Добавьте idempotency-проверку в обработчик."
        ),
    },
    {
        "id": "db_foreign_key",
        "category": "db",
        "match_type": ["ForeignKeyViolationError", "IntegrityError"],
        "patterns": [
            "ForeignKeyViolationError",
            "foreign key constraint",
            "FOREIGN KEY constraint failed",
        ],
        "title": "Нарушение внешнего ключа в БД",
        "description": "Ссылка на несуществующую запись (например, заказ без клиента).",
        "solution": (
            "1. Убедитесь, что родительская запись существует перед созданием дочерней.\n"
            "2. Добавьте проверку `if parent is None: return` перед операцией.\n"
            "3. Проверьте каскадное удаление в моделях SQLAlchemy."
        ),
    },
    # ── Telegram API ──────────────────────────────────────────────────────────
    {
        "id": "tg_message_not_modified",
        "category": "telegram",
        "match_type": ["TelegramBadRequest"],
        "patterns": [
            "message is not modified",
            "MESSAGE_NOT_MODIFIED",
        ],
        "title": "Telegram: сообщение не изменено",
        "description": "Попытка отредактировать сообщение с тем же содержимым.",
        "solution": (
            "1. Добавьте сравнение нового и старого содержимого перед вызовом `edit_text`.\n"
            "2. Используйте вспомогательную функцию `safe_edit_message()` — она уже перехватывает эту ошибку.\n"
            "3. Убедитесь, что `safe_edit_message` импортируется и используется во всех обработчиках."
        ),
    },
    {
        "id": "tg_chat_not_found",
        "category": "telegram",
        "match_type": ["TelegramBadRequest", "TelegramForbiddenError"],
        "patterns": [
            "chat not found",
            "CHAT_NOT_FOUND",
            "user is deactivated",
            "bot was blocked by the user",
            "Forbidden: bot was blocked",
        ],
        "title": "Telegram: чат/пользователь недоступен",
        "description": "Пользователь заблокировал бота или удалил аккаунт.",
        "solution": (
            "1. Оберните отправку сообщений пользователям в try/except TelegramForbiddenError.\n"
            "2. При ошибке помечайте пользователя как `is_active=False` в БД.\n"
            "3. Не пытайтесь повторно отправить сообщение такому пользователю."
        ),
    },
    {
        "id": "tg_message_to_delete_not_found",
        "category": "telegram",
        "match_type": ["TelegramBadRequest"],
        "patterns": [
            "message to delete not found",
            "message to forward not found",
            "message to copy not found",
        ],
        "title": "Telegram: сообщение для удаления/пересылки не найдено",
        "description": "Сообщение уже было удалено или недоступно.",
        "solution": (
            "1. Оберните `delete_message` / `forward_message` в try/except.\n"
            "2. При ошибке просто логируйте предупреждение и продолжайте выполнение.\n"
            "3. В таблице `scheduled_posts` обновляйте статус поста даже при ошибке удаления."
        ),
    },
    {
        "id": "tg_rate_limit",
        "category": "telegram",
        "match_type": ["TelegramRetryAfter", "RetryAfter"],
        "patterns": [
            "RetryAfter",
            "Too Many Requests",
            "retry_after",
            "FLOOD_WAIT",
        ],
        "title": "Telegram: превышен лимит запросов (flood)",
        "description": "Бот отправляет слишком много сообщений за короткое время.",
        "solution": (
            "1. Используйте asyncio.sleep(retry_after) при получении RetryAfter.\n"
            "2. Добавьте задержку между рассылками (минимум 0.05–0.1 сек на сообщение).\n"
            "3. Для массовых рассылок используйте очередь с ограничением скорости."
        ),
    },
    # ── Конфигурация ──────────────────────────────────────────────────────────
    {
        "id": "config_bot_token_missing",
        "category": "config",
        "match_type": [],
        "patterns": [
            "BOT_TOKEN не установлен",
            "Unauthorized",
            "401: Unauthorized",
        ],
        "title": "BOT_TOKEN не задан или неверен",
        "description": "Переменная окружения BOT_TOKEN отсутствует или содержит неверный токен.",
        "solution": (
            "1. Установите переменную окружения BOT_TOKEN (получите у @BotFather).\n"
            "2. Убедитесь, что файл .env загружается (python-dotenv).\n"
            "3. Не используйте токены от удалённых ботов."
        ),
    },
    {
        "id": "config_default_password",
        "category": "config",
        "match_type": [],
        "patterns": [
            "admin123",
            "небезопасное значение по умолчанию",
        ],
        "title": "Небезопасный пароль администратора",
        "description": "Используется пароль по умолчанию 'admin123'.",
        "solution": (
            "1. Установите переменную окружения ADMIN_PASSWORD со сложным паролем.\n"
            "2. Пароль должен содержать не менее 12 символов, цифры и спецсимволы."
        ),
    },
    # ── Платёжный процесс ─────────────────────────────────────────────────────
    {
        "id": "payment_slot_not_freed",
        "category": "payment",
        "match_type": [],
        "patterns": [
            "slot.*reserved",
            "слот.*зарезервирован",
        ],
        "title": "Слот не освобождён после отмены",
        "description": "При отклонении оплаты слот оставался в статусе 'reserved'.",
        "solution": (
            "1. В обработчике adm_reject_payment: после order.status='cancelled' "
            "дополнительно устанавливайте slot.status='available', "
            "slot.reserved_by=None, slot.reserved_until=None.\n"
            "2. Используйте одну транзакцию для обеих операций."
        ),
    },
    # ── Дата и время ─────────────────────────────────────────────────────────
    {
        "id": "datetime_utcnow_deprecated",
        "category": "datetime",
        "match_type": ["DeprecationWarning"],
        "patterns": [
            "datetime.utcnow",
            "DeprecationWarning.*utcnow",
        ],
        "title": "Устаревший datetime.utcnow()",
        "description": "datetime.utcnow() устарел в Python 3.12.",
        "solution": (
            "1. Используйте utc_now() из utils/helpers.py.\n"
            "2. utc_now() = datetime.now(timezone.utc).replace(tzinfo=None).\n"
            "3. Замените все вызовы datetime.utcnow() на utc_now()."
        ),
    },
    # ── AI / Claude API ───────────────────────────────────────────────────────
    {
        "id": "claude_api_key_invalid",
        "category": "ai",
        "match_type": [],
        "patterns": [
            "401",
            "invalid_api_key",
            "authentication_error",
            "Claude API: неверный ключ",
        ],
        "title": "Неверный ключ Claude API",
        "description": "CLAUDE_API_KEY недействителен или истёк.",
        "solution": (
            "1. Проверьте значение переменной окружения CLAUDE_API_KEY.\n"
            "2. Убедитесь, что ключ не был отозван в https://console.anthropic.com/.\n"
            "3. Проверьте баланс аккаунта Anthropic."
        ),
    },
    {
        "id": "claude_api_timeout",
        "category": "ai",
        "match_type": ["asyncio.TimeoutError", "TimeoutError"],
        "patterns": [
            "Claude API.*таймаут",
            "TimeoutError",
        ],
        "title": "Таймаут Claude API",
        "description": "Claude API не ответил в отведённое время.",
        "solution": (
            "1. Уменьшите max_tokens в запросе к API.\n"
            "2. Увеличьте таймаут (aiohttp.ClientTimeout) при необходимости.\n"
            "3. Добавьте повтор запроса с экспоненциальной задержкой."
        ),
    },
    # ── Аутентификация ────────────────────────────────────────────────────────
    {
        "id": "auth_not_authenticated",
        "category": "auth",
        "match_type": [],
        "patterns": [
            "Требуется авторизация",
            "not in authenticated_admins",
        ],
        "title": "Доступ без авторизации",
        "description": "Пользователь попытался выполнить административное действие без входа.",
        "solution": (
            "1. Все административные callback-обработчики должны начинаться с проверки "
            "authenticated_admins / ADMIN_IDS.\n"
            "2. Используйте единый декоратор или guard-функцию для проверки прав."
        ),
    },
    # ── JSON / данные ─────────────────────────────────────────────────────────
    {
        "id": "json_decode_error",
        "category": "data",
        "match_type": ["JSONDecodeError", "ValueError"],
        "patterns": [
            "JSONDecodeError",
            "json.decoder",
            "Expecting value",
        ],
        "title": "Ошибка разбора JSON",
        "description": "Невалидный JSON в поле БД или ответе API.",
        "solution": (
            "1. Оберните json.loads() в try/except json.JSONDecodeError.\n"
            "2. Проверяйте данные перед записью в БД.\n"
            "3. При ошибке используйте fallback-значение (None или пустой список)."
        ),
    },
    # ── Голое исключение ──────────────────────────────────────────────────────
    {
        "id": "bare_except",
        "category": "code_quality",
        "match_type": [],
        "patterns": [
            "except:",
            "bare except",
        ],
        "title": "Голый except без типа",
        "description": "Bare except: перехватывает SystemExit и KeyboardInterrupt.",
        "solution": (
            "1. Замените `except:` на `except Exception:`.\n"
            "2. Для критичных ошибок используйте конкретные типы (OperationalError и т.п.)."
        ),
    },
]


def lookup_error(
    exc: BaseException,
    traceback_text: str = "",
) -> Optional[dict]:
    """Найти подходящий шаблон ошибки в библиотеке.

    Проверяет тип исключения и наличие ключевых слов в трейсбеке/сообщении.
    Возвращает первый подходящий шаблон или None.
    """
    exc_type = type(exc).__name__
    exc_msg = str(exc)
    combined = f"{exc_type}: {exc_msg}\n{traceback_text}"

    for entry in KNOWN_ERRORS:
        # Проверяем тип исключения (если указан)
        type_match = (
            not entry["match_type"]
            or exc_type in entry["match_type"]
            or any(t in combined for t in entry["match_type"])
        )
        if not type_match:
            continue

        # Проверяем шаблоны (хватит одного совпадения)
        for pattern in entry["patterns"]:
            try:
                if re.search(pattern, combined, re.IGNORECASE):
                    return entry
            except re.error:
                if pattern.lower() in combined.lower():
                    return entry

    return None


def record_unknown_error(
    exc: BaseException,
    traceback_text: str,
    context: str = "",
) -> None:
    """Записать неизвестную ошибку в файл журнала для последующего анализа.

    Файл ERROR_LOG_PATH содержит список JSON-объектов.
    При превышении ERROR_LOG_MAX_ENTRIES старые записи удаляются.
    """
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "exc_type": type(exc).__name__,
        "exc_msg": str(exc)[:300],
        "traceback": traceback_text[-1000:],
        "context": context[:200],
    }

    try:
        existing: list = []
        if ERROR_LOG_PATH.exists():
            try:
                existing = json.loads(ERROR_LOG_PATH.read_text(encoding="utf-8"))
                if not isinstance(existing, list):
                    existing = []
            except (json.JSONDecodeError, OSError):
                existing = []

        existing.append(entry)

        # Обрезаем старые записи
        if len(existing) > ERROR_LOG_MAX_ENTRIES:
            existing = existing[-ERROR_LOG_MAX_ENTRIES:]

        ERROR_LOG_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Не удалось записать неизвестную ошибку в журнал: {e}")


def get_error_log(limit: int = 20) -> list[dict]:
    """Вернуть последние `limit` записей из файла журнала неизвестных ошибок."""
    try:
        if not ERROR_LOG_PATH.exists():
            return []
        entries = json.loads(ERROR_LOG_PATH.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            return []
        return entries[-limit:][::-1]  # Последние сверху
    except Exception as e:
        logger.warning(f"Не удалось прочитать журнал ошибок: {e}")
        return []


def format_known_error(entry: dict) -> str:
    """Форматировать известный шаблон ошибки для вывода в Telegram."""
    lines = [
        f"📖 *{entry['title']}*",
        f"🏷 Категория: `{entry['category']}`",
        f"📝 {entry['description']}",
        "",
        f"✅ *Решение:*\n{entry['solution']}",
    ]
    return "\n".join(lines)

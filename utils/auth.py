"""
Утилиты для аутентификации и защиты от брутфорса.

Содержит чистые Python-классы без зависимостей от aiogram/database,
поэтому легко тестируются в изоляции.
"""
from datetime import datetime, timedelta

from utils.helpers import utc_now


# Настройки по умолчанию
BRUTE_FORCE_MAX_ATTEMPTS: int = 5
BRUTE_FORCE_WINDOW: timedelta = timedelta(minutes=5)


class BruteForceGuard:
    """Отслеживает неудачные попытки ввода пароля и блокирует user_id на время.

    Логика:
      - Если за последние ``window`` минут было >= ``max_attempts`` провальных
        попыток — учётная запись считается заблокированной до истечения окна.
      - При успешном входе история очищается.
      - Хранит только временны́е метки, не сам ввод пользователя.

    Пример::

        guard = BruteForceGuard()
        if guard.is_locked(user_id):
            ...  # отказать
        elif entered == correct_password:
            guard.clear(user_id)
            ...  # пустить
        else:
            guard.record_failure(user_id)
            ...  # сообщить об ошибке
    """

    def __init__(
        self,
        max_attempts: int = BRUTE_FORCE_MAX_ATTEMPTS,
        window: timedelta = BRUTE_FORCE_WINDOW,
    ) -> None:
        self._max = max_attempts
        self._window = window
        # user_id → список UTC-datetime неудачных попыток
        self._failures: dict[int, list[datetime]] = {}

    def _prune(self, user_id: int) -> list[datetime]:
        """Удалить устаревшие записи и вернуть актуальные."""
        cutoff = utc_now() - self._window
        recent = [t for t in self._failures.get(user_id, []) if t > cutoff]
        self._failures[user_id] = recent
        return recent

    def is_locked(self, user_id: int) -> bool:
        """Вернуть True, если user_id сейчас заблокирован."""
        return len(self._prune(user_id)) >= self._max

    def record_failure(self, user_id: int) -> int:
        """Записать неудачную попытку. Возвращает текущее количество попыток."""
        self._prune(user_id)
        self._failures.setdefault(user_id, []).append(utc_now())
        return len(self._failures[user_id])

    def clear(self, user_id: int) -> None:
        """Очистить историю попыток после успешного входа."""
        self._failures.pop(user_id, None)

    def unlock_in(self, user_id: int) -> timedelta:
        """Время до снятия блокировки (timedelta(0) если не заблокирован)."""
        attempts = self._prune(user_id)
        if not attempts:
            return timedelta(0)
        unlock_at = min(attempts) + self._window
        remaining = unlock_at - utc_now()
        return max(timedelta(0), remaining)

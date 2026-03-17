"""
Сервис самодиагностики и AI-улучшений бота
"""
import asyncio
import logging
import time
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Optional

import aiohttp
from sqlalchemy import select, func, text

from config import (
    BOT_TOKEN, CLAUDE_API_KEY, CLAUDE_MODEL, TELEMETR_API_TOKEN, TELEMETR_API_URL,
    ADMIN_IDS, ADMIN_PASSWORD, MAX_BOT_TOKEN, AUTOPOST_ENABLED, DATABASE_URL,
)
from database import (
    async_session_maker, Channel, Manager, Order, ScheduledPost, Client,
    PostAnalytics, Slot, CategoryCPM, Competition, PromoCode,
)


logger = logging.getLogger(__name__)


async def run_diagnostics() -> dict:
    """Запустить полную диагностику всех компонентов бота.

    Возвращает словарь с результатами проверок:
      - db: (icon, message)
      - claude: (icon, message)
      - telemetr: (icon, message)
      - queue: dict с pending_posts / overdue_posts / pending_payments  или None при ошибке
    """
    results = {}

    # 1. База данных
    try:
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
        results["db"] = ("🟢", "База данных доступна")
    except Exception as e:
        results["db"] = ("🔴", f"База данных недоступна: {str(e)[:80]}")

    # 2. Claude API
    if CLAUDE_API_KEY:
        try:
            headers = {
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": CLAUDE_MODEL,
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "ping"}],
            }
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                async with http_session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload,
                ) as resp:
                    if resp.status == 200:
                        results["claude"] = ("🟢", "Claude API отвечает")
                    elif resp.status == 401:
                        results["claude"] = ("🔴", "Claude API: неверный ключ")
                    else:
                        results["claude"] = ("🟡", f"Claude API: код {resp.status}")
        except asyncio.TimeoutError:
            results["claude"] = ("🟡", "Claude API: таймаут")
        except Exception as e:
            results["claude"] = ("🔴", f"Claude API ошибка: {str(e)[:60]}")
    else:
        results["claude"] = ("🔴", "Claude API: ключ не настроен")

    # 3. Telemetr API
    if TELEMETR_API_TOKEN:
        try:
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                async with http_session.get(
                    f"{TELEMETR_API_URL}/channels",
                    headers={"Authorization": f"Bearer {TELEMETR_API_TOKEN}"},
                    params={"limit": 1},
                ) as resp:
                    if resp.status in (200, 404):
                        results["telemetr"] = ("🟢", "Telemetr API отвечает")
                    elif resp.status == 401:
                        results["telemetr"] = ("🔴", "Telemetr API: неверный токен")
                    else:
                        results["telemetr"] = ("🟡", f"Telemetr API: код {resp.status}")
        except asyncio.TimeoutError:
            results["telemetr"] = ("🟡", "Telemetr API: таймаут")
        except Exception as e:
            results["telemetr"] = ("🔴", f"Telemetr ошибка: {str(e)[:60]}")
    else:
        results["telemetr"] = ("🔴", "Telemetr API: токен не настроен")

    # 4. Очередь и ожидающие действия
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with async_session_maker() as session:
            pending_posts = (await session.execute(
                select(func.count(ScheduledPost.id))
                .where(ScheduledPost.status == "pending")
            )).scalar() or 0

            overdue_posts = (await session.execute(
                select(func.count(ScheduledPost.id))
                .where(
                    ScheduledPost.status == "pending",
                    ScheduledPost.scheduled_time < now,
                )
            )).scalar() or 0

            pending_payments = (await session.execute(
                select(func.count(Order.id))
                .where(Order.status == "payment_uploaded")
            )).scalar() or 0

            moderation_posts = (await session.execute(
                select(func.count(ScheduledPost.id))
                .where(ScheduledPost.status == "moderation")
            )).scalar() or 0

        results["queue"] = {
            "pending_posts": pending_posts,
            "overdue_posts": overdue_posts,
            "pending_payments": pending_payments,
            "moderation_posts": moderation_posts,
        }
    except Exception as e:
        logger.error(f"Diagnostics queue check error: {e}")
        results["queue"] = None

    return results


async def run_deep_diagnostics() -> dict:
    """Запустить углублённую диагностику всех разделов и компонентов бота.

    Возвращает словарь:
      - config: dict[name -> (icon, message)]
      - db_tables: dict[name -> (icon, message)]
      - active_data: dict[name -> (icon, message)]
      - services: dict[name -> (icon, message)]
      - apis: dict[name -> (icon, message, latency_ms)]
      - sections: dict[name -> (icon, message)]
      - summary: (total, ok, warn, error)
    """
    report: dict = {
        "config": {},
        "db_tables": {},
        "active_data": {},
        "services": {},
        "apis": {},
        "sections": {},
    }

    # ─── 1. Конфигурация ────────────────────────────────────────────────────
    report["config"]["BOT_TOKEN"] = (
        ("🟢", "BOT_TOKEN задан") if BOT_TOKEN else ("🔴", "BOT_TOKEN не задан")
    )
    report["config"]["ADMIN_IDS"] = (
        ("🟢", f"ADMIN_IDS: {len(ADMIN_IDS)} шт.") if ADMIN_IDS else ("🔴", "ADMIN_IDS пусто")
    )
    report["config"]["ADMIN_PASSWORD"] = (
        ("🟡", "Пароль по умолчанию (небезопасно)")
        if ADMIN_PASSWORD == "admin123"
        else ("🟢", "Пароль задан")
    )
    report["config"]["CLAUDE_API_KEY"] = (
        ("🟢", "CLAUDE_API_KEY задан") if CLAUDE_API_KEY else ("🟡", "CLAUDE_API_KEY не задан")
    )
    report["config"]["TELEMETR_TOKEN"] = (
        ("🟢", "TELEMETR_API_TOKEN задан") if TELEMETR_API_TOKEN else ("🟡", "TELEMETR_API_TOKEN не задан")
    )
    report["config"]["MAX_BOT_TOKEN"] = (
        ("🟢", "MAX_BOT_TOKEN задан") if MAX_BOT_TOKEN else ("🟡", "MAX_BOT_TOKEN не задан")
    )
    report["config"]["AUTOPOST"] = (
        ("🟢", "Автопостинг включён") if AUTOPOST_ENABLED else ("🟡", "Автопостинг выключен")
    )

    # ─── 2. Таблицы базы данных ─────────────────────────────────────────────
    table_checks = [
        ("channels", Channel),
        ("managers", Manager),
        ("clients", Client),
        ("orders", Order),
        ("slots", Slot),
        ("scheduled_posts", ScheduledPost),
        ("competitions", Competition),
        ("post_analytics", PostAnalytics),
        ("promo_codes", PromoCode),
        ("category_cpm", CategoryCPM),
    ]
    for table_name, model in table_checks:
        try:
            async with async_session_maker() as session:
                cnt = (await session.execute(select(func.count(model.id)))).scalar() or 0
            report["db_tables"][table_name] = ("🟢", f"{table_name}: {cnt} записей")
        except Exception as e:
            report["db_tables"][table_name] = ("🔴", f"{table_name}: ошибка — {str(e)[:60]}")

    # ─── 3. Активные данные (критичные для работы) ──────────────────────────
    try:
        async with async_session_maker() as session:
            active_ch = (await session.execute(
                select(func.count(Channel.id)).where(Channel.is_active == True)
            )).scalar() or 0
        report["active_data"]["active_channels"] = (
            ("🟢", f"Активных каналов: {active_ch}")
            if active_ch > 0
            else ("🔴", "Нет активных каналов — клиенты не могут бронировать рекламу!")
        )
    except Exception as e:
        report["active_data"]["active_channels"] = ("🔴", f"Ошибка проверки каналов: {str(e)[:60]}")

    try:
        async with async_session_maker() as session:
            avail_slots = (await session.execute(
                select(func.count(Slot.id)).where(
                    Slot.status == "available",
                    Slot.slot_date >= date_type.today(),
                )
            )).scalar() or 0
        report["active_data"]["available_slots"] = (
            ("🟢", f"Доступных слотов: {avail_slots}")
            if avail_slots > 0
            else ("🟡", "Нет доступных слотов — клиентам нечего бронировать")
        )
    except Exception as e:
        report["active_data"]["available_slots"] = ("🔴", f"Ошибка проверки слотов: {str(e)[:60]}")

    try:
        async with async_session_maker() as session:
            active_mgr = (await session.execute(
                select(func.count(Manager.id)).where(Manager.is_active == True)
            )).scalar() or 0
        report["active_data"]["active_managers"] = (
            ("🟢", f"Активных менеджеров: {active_mgr}")
            if active_mgr > 0
            else ("🟡", "Нет активных менеджеров")
        )
    except Exception as e:
        report["active_data"]["active_managers"] = ("🔴", f"Ошибка проверки менеджеров: {str(e)[:60]}")

    try:
        async with async_session_maker() as session:
            active_promo = (await session.execute(
                select(func.count(PromoCode.id)).where(PromoCode.is_active == True)
            )).scalar() or 0
        report["active_data"]["active_promos"] = ("🟢", f"Активных промокодов: {active_promo}")
    except Exception as e:
        report["active_data"]["active_promos"] = ("🔴", f"Ошибка проверки промокодов: {str(e)[:60]}")

    # ─── 4. Сервисы метрик ───────────────────────────────────────────────────
    from services.metrics import (
        get_sales_metrics, get_channel_metrics, get_manager_metrics,
        get_client_metrics, get_format_metrics, get_post_analytics_metrics,
    )
    metric_checks = [
        ("sales_metrics", get_sales_metrics),
        ("channel_metrics", get_channel_metrics),
        ("manager_metrics", get_manager_metrics),
        ("client_metrics", get_client_metrics),
        ("format_metrics", get_format_metrics),
        ("post_analytics_metrics", get_post_analytics_metrics),
    ]
    for svc_name, svc_fn in metric_checks:
        try:
            t0 = time.monotonic()
            result = await svc_fn()
            elapsed = int((time.monotonic() - t0) * 1000)
            if result is not None:
                report["services"][svc_name] = ("🟢", f"{svc_name}: OK ({elapsed} мс)")
            else:
                report["services"][svc_name] = ("🔴", f"{svc_name}: вернул None")
        except Exception as e:
            report["services"][svc_name] = ("🔴", f"{svc_name}: {str(e)[:60]}")

    # ─── 5. Внешние API (с замером времени) ─────────────────────────────────
    # Database latency
    try:
        t0 = time.monotonic()
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
        elapsed = int((time.monotonic() - t0) * 1000)
        report["apis"]["db"] = ("🟢", f"База данных: {elapsed} мс")
    except Exception as e:
        report["apis"]["db"] = ("🔴", f"База данных: {str(e)[:60]}")

    # Claude API
    if CLAUDE_API_KEY:
        try:
            t0 = time.monotonic()
            headers = {
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            payload = {
                "model": CLAUDE_MODEL,
                "max_tokens": 5,
                "messages": [{"role": "user", "content": "ping"}],
            }
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                async with http_session.post(
                    "https://api.anthropic.com/v1/messages",
                    headers=headers,
                    json=payload,
                ) as resp:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    if resp.status == 200:
                        report["apis"]["claude"] = ("🟢", f"Claude API: {elapsed} мс")
                    elif resp.status == 401:
                        report["apis"]["claude"] = ("🔴", "Claude API: неверный ключ")
                    else:
                        report["apis"]["claude"] = ("🟡", f"Claude API: код {resp.status}")
        except asyncio.TimeoutError:
            report["apis"]["claude"] = ("🟡", "Claude API: таймаут (>10 с)")
        except Exception as e:
            report["apis"]["claude"] = ("🔴", f"Claude API: {str(e)[:60]}")
    else:
        report["apis"]["claude"] = ("🔴", "Claude API: ключ не задан")

    # Telemetr API
    if TELEMETR_API_TOKEN:
        try:
            t0 = time.monotonic()
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                async with http_session.get(
                    f"{TELEMETR_API_URL}/channels",
                    headers={"Authorization": f"Bearer {TELEMETR_API_TOKEN}"},
                    params={"limit": 1},
                ) as resp:
                    elapsed = int((time.monotonic() - t0) * 1000)
                    if resp.status in (200, 404):
                        report["apis"]["telemetr"] = ("🟢", f"Telemetr API: {elapsed} мс")
                    elif resp.status == 401:
                        report["apis"]["telemetr"] = ("🔴", "Telemetr API: неверный токен")
                    else:
                        report["apis"]["telemetr"] = ("🟡", f"Telemetr API: код {resp.status}")
        except asyncio.TimeoutError:
            report["apis"]["telemetr"] = ("🟡", "Telemetr API: таймаут (>8 с)")
        except Exception as e:
            report["apis"]["telemetr"] = ("🔴", f"Telemetr API: {str(e)[:60]}")
    else:
        report["apis"]["telemetr"] = ("🔴", "Telemetr API: токен не задан")

    # ─── 6. Разделы (имитация обращения к данным каждого раздела) ──────────
    section_checks = [
        ("📢 Каналы", _check_section_channels),
        ("💳 Оплаты", _check_section_payments),
        ("📝 Модерация", _check_section_moderation),
        ("👥 Менеджеры", _check_section_managers),
        ("🏆 Соревнования", _check_section_competitions),
        ("💰 CPM тематик", _check_section_cpm),
        ("📅 Автопостинг", _check_section_autoposting),
        ("🎟 Промокоды", _check_section_promos),
        ("🧑‍💼 Клиенты (метрики)", _check_section_client_metrics),
        ("🛒 Бронирование клиентов", _check_section_booking),
    ]
    for sect_name, sect_fn in section_checks:
        try:
            icon, msg = await sect_fn()
            report["sections"][sect_name] = (icon, msg)
        except Exception as e:
            report["sections"][sect_name] = ("🔴", f"{sect_name}: непредвиденная ошибка — {str(e)[:60]}")

    # ─── Итоговая сводка ────────────────────────────────────────────────────
    all_items = (
        list(report["config"].values())
        + list(report["db_tables"].values())
        + list(report["active_data"].values())
        + list(report["services"].values())
        + list(report["apis"].values())
        + list(report["sections"].values())
    )
    total = len(all_items)
    ok = sum(1 for icon, _ in all_items if icon == "🟢")
    warn = sum(1 for icon, _ in all_items if icon == "🟡")
    error = sum(1 for icon, _ in all_items if icon == "🔴")
    report["summary"] = (total, ok, warn, error)

    return report


# ─── Вспомогательные проверки разделов ────────────────────────────────────

async def _check_section_channels() -> tuple[str, str]:
    async with async_session_maker() as session:
        total = (await session.execute(select(func.count(Channel.id)))).scalar() or 0
        active = (await session.execute(
            select(func.count(Channel.id)).where(Channel.is_active == True)
        )).scalar() or 0
    if total == 0:
        return ("🟡", "Нет каналов")
    return ("🟢", f"Каналов: {total} ({active} активных)")


async def _check_section_payments() -> tuple[str, str]:
    async with async_session_maker() as session:
        pending = (await session.execute(
            select(func.count(Order.id)).where(Order.status == "payment_uploaded")
        )).scalar() or 0
    return ("🟢", f"Оплат ожидают подтверждения: {pending}")


async def _check_section_moderation() -> tuple[str, str]:
    async with async_session_maker() as session:
        moderation = (await session.execute(
            select(func.count(ScheduledPost.id)).where(ScheduledPost.status == "moderation")
        )).scalar() or 0
    return ("🟢", f"На модерации: {moderation}")


async def _check_section_managers() -> tuple[str, str]:
    async with async_session_maker() as session:
        total = (await session.execute(select(func.count(Manager.id)))).scalar() or 0
        active = (await session.execute(
            select(func.count(Manager.id)).where(Manager.is_active == True)
        )).scalar() or 0
    if total == 0:
        return ("🟡", "Нет менеджеров")
    return ("🟢", f"Менеджеров: {total} ({active} активных)")


async def _check_section_competitions() -> tuple[str, str]:
    async with async_session_maker() as session:
        total = (await session.execute(select(func.count(Competition.id)))).scalar() or 0
    return ("🟢", f"Соревнований: {total}")


async def _check_section_cpm() -> tuple[str, str]:
    async with async_session_maker() as session:
        total = (await session.execute(select(func.count(CategoryCPM.id)))).scalar() or 0
    return ("🟢", f"Записей CPM: {total}")


async def _check_section_autoposting() -> tuple[str, str]:
    if not AUTOPOST_ENABLED:
        return ("🟡", "Автопостинг отключён в конфиге")
    async with async_session_maker() as session:
        pending = (await session.execute(
            select(func.count(ScheduledPost.id)).where(ScheduledPost.status == "pending")
        )).scalar() or 0
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        overdue = (await session.execute(
            select(func.count(ScheduledPost.id)).where(
                ScheduledPost.status == "pending",
                ScheduledPost.scheduled_time < now,
            )
        )).scalar() or 0
    if overdue > 0:
        return ("🟡", f"Постов: {pending} в очереди, {overdue} просрочено")
    return ("🟢", f"Постов в очереди: {pending}")


async def _check_section_promos() -> tuple[str, str]:
    async with async_session_maker() as session:
        total = (await session.execute(select(func.count(PromoCode.id)))).scalar() or 0
        active = (await session.execute(
            select(func.count(PromoCode.id)).where(PromoCode.is_active == True)
        )).scalar() or 0
    return ("🟢", f"Промокодов: {total} ({active} активных)")


async def _check_section_client_metrics() -> tuple[str, str]:
    from services.metrics import get_client_metrics
    data = await get_client_metrics()
    if data is None:
        return ("🔴", "Сервис метрик клиентов вернул ошибку")
    return ("🟢", f"Клиентов в БД: {data['total']}")


async def _check_section_booking() -> tuple[str, str]:
    async with async_session_maker() as session:
        active_ch = (await session.execute(
            select(func.count(Channel.id)).where(Channel.is_active == True)
        )).scalar() or 0
        avail_slots = (await session.execute(
            select(func.count(Slot.id)).where(
                Slot.status == "available",
                Slot.slot_date >= date_type.today(),
            )
        )).scalar() or 0
    if active_ch == 0:
        return ("🔴", "Нет активных каналов для бронирования")
    if avail_slots == 0:
        return ("🟡", f"Каналов: {active_ch}, но нет доступных слотов")
    return ("🟢", f"Готово: {active_ch} каналов, {avail_slots} слотов")


async def gather_business_metrics() -> dict:
    """Собрать бизнес-метрики для AI-анализа."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    month_ago = now - timedelta(days=30)
    prev_month_start = now - timedelta(days=60)
    week_ago = now - timedelta(days=7)

    metrics: dict = {}
    try:
        async with async_session_maker() as session:
            # Выручка
            revenue_month = float((await session.execute(
                select(func.sum(Order.final_price)).where(
                    Order.status == "payment_confirmed",
                    Order.paid_at >= month_ago,
                )
            )).scalar() or 0)

            revenue_prev_month = float((await session.execute(
                select(func.sum(Order.final_price)).where(
                    Order.status == "payment_confirmed",
                    Order.paid_at >= prev_month_start,
                    Order.paid_at < month_ago,
                )
            )).scalar() or 0)

            # Заказы
            total_orders = (await session.execute(
                select(func.count(Order.id))
            )).scalar() or 0

            confirmed_orders = (await session.execute(
                select(func.count(Order.id)).where(Order.status == "payment_confirmed")
            )).scalar() or 0

            cancelled_orders = (await session.execute(
                select(func.count(Order.id)).where(Order.status == "cancelled")
            )).scalar() or 0

            orders_week = (await session.execute(
                select(func.count(Order.id)).where(Order.created_at >= week_ago)
            )).scalar() or 0

            # Каналы
            total_channels = (await session.execute(
                select(func.count(Channel.id))
            )).scalar() or 0

            active_channels = (await session.execute(
                select(func.count(Channel.id)).where(Channel.is_active == True)
            )).scalar() or 0

            # Менеджеры
            active_managers = (await session.execute(
                select(func.count(Manager.id)).where(Manager.is_active == True)
            )).scalar() or 0

            # Клиенты
            total_clients = (await session.execute(
                select(func.count(Client.id))
            )).scalar() or 0

            new_clients_month = (await session.execute(
                select(func.count(Client.id)).where(Client.created_at >= month_ago)
            )).scalar() or 0

            # Автопостинг
            pending_posts = (await session.execute(
                select(func.count(ScheduledPost.id)).where(ScheduledPost.status == "pending")
            )).scalar() or 0

            # Аналитика постов
            analytics_count = (await session.execute(
                select(func.count(PostAnalytics.id))
            )).scalar() or 0

        conversion_rate = round(confirmed_orders / total_orders * 100, 1) if total_orders > 0 else 0
        cancel_rate = round(cancelled_orders / total_orders * 100, 1) if total_orders > 0 else 0
        revenue_change = (
            round((revenue_month - revenue_prev_month) / revenue_prev_month * 100, 1)
            if revenue_prev_month > 0 else None
        )

        metrics = {
            "revenue_month": revenue_month,
            "revenue_prev_month": revenue_prev_month,
            "revenue_change_pct": revenue_change,
            "total_orders": total_orders,
            "orders_week": orders_week,
            "conversion_rate_pct": conversion_rate,
            "cancel_rate_pct": cancel_rate,
            "active_channels": active_channels,
            "total_channels": total_channels,
            "active_managers": active_managers,
            "total_clients": total_clients,
            "new_clients_month": new_clients_month,
            "pending_autopost": pending_posts,
            "analytics_records": analytics_count,
        }
    except Exception as e:
        logger.error(f"Error gathering business metrics: {e}")

    return metrics


async def get_improvement_suggestions(metrics: dict) -> Optional[str]:
    """Получить AI-рекомендации по улучшению работы бота на основе метрик."""
    if not CLAUDE_API_KEY:
        return "⚠️ Claude API не настроен. Добавьте CLAUDE_API_KEY для получения AI-рекомендаций."

    if not metrics:
        return "⚠️ Нет данных для анализа. Добавьте заказы и клиентов для получения рекомендаций."

    # Формируем текстовое резюме метрик
    parts = ["Метрики рекламного CRM-бота за последние 30 дней:"]
    parts.append(f"- Выручка (текущий месяц): {metrics.get('revenue_month', 0):,.0f}₽")
    parts.append(f"- Выручка (прошлый месяц): {metrics.get('revenue_prev_month', 0):,.0f}₽")

    change = metrics.get("revenue_change_pct")
    if change is not None:
        direction = "▲" if change >= 0 else "▼"
        parts.append(f"- Изменение выручки м/м: {direction}{abs(change):.1f}%")

    parts.append(f"- Новых заказов за неделю: {metrics.get('orders_week', 0)}")
    parts.append(f"- Конверсия заказов в оплату: {metrics.get('conversion_rate_pct', 0):.1f}%")
    parts.append(f"- Доля отмён: {metrics.get('cancel_rate_pct', 0):.1f}%")
    parts.append(
        f"- Активных каналов: {metrics.get('active_channels', 0)} из {metrics.get('total_channels', 0)}"
    )
    parts.append(f"- Активных менеджеров: {metrics.get('active_managers', 0)}")
    parts.append(
        f"- Всего клиентов: {metrics.get('total_clients', 0)} "
        f"(новых за месяц: {metrics.get('new_clients_month', 0)})"
    )
    parts.append(f"- Запланированных постов в очереди: {metrics.get('pending_autopost', 0)}")
    parts.append(f"- Записей аналитики постов: {metrics.get('analytics_records', 0)}")

    data_text = "\n".join(parts)

    system_prompt = (
        "Ты — бизнес-аналитик и эксперт по Telegram-рекламным агентствам. "
        "На основе метрик CRM-бота для продажи рекламы в Telegram дай конкретные рекомендации "
        "по улучшению работы:\n"
        "1. Какие метрики вызывают беспокойство и почему\n"
        "2. Конкретные шаги по росту выручки\n"
        "3. Как улучшить конверсию и снизить долю отмён\n"
        "4. Рекомендации по работе с менеджерами и каналами\n"
        "Будь конкретным и практичным. Ответ до 800 символов. Используй эмодзи и чёткую структуру."
    )

    try:
        headers = {
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": CLAUDE_MODEL,
            "max_tokens": 600,
            "system": system_prompt,
            "messages": [{"role": "user", "content": data_text}],
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            async with http_session.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    suggestion_text = data["content"][0]["text"]
                    # Сохраняем AI-рекомендацию в журнал улучшений
                    try:
                        from services.improvement_log import log_improvement
                        log_improvement(
                            title="AI-анализ бизнес-метрик",
                            description=suggestion_text,
                            category="ai_suggestion",
                            author="ai",
                        )
                    except Exception as _log_err:
                        logger.warning(f"Не удалось сохранить AI-рекомендацию в журнал: {_log_err}")
                    return suggestion_text
                else:
                    error = await resp.text()
                    logger.error(f"Claude API error (improvement suggestions): {resp.status} - {error}")
                    return None
    except asyncio.TimeoutError:
        logger.error("Claude API timeout (improvement suggestions)")
        return "⏱ Таймаут при обращении к AI. Попробуйте ещё раз."
    except Exception as e:
        logger.error(f"Improvement suggestions AI error: {e}")
        return None

"""
Сервис самодиагностики и AI-улучшений бота
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from sqlalchemy import select, func, text

from config import CLAUDE_API_KEY, CLAUDE_MODEL, TELEMETR_API_TOKEN, TELEMETR_API_URL
from database import (
    async_session_maker, Channel, Manager, Order, ScheduledPost, Client, PostAnalytics
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
                    return data["content"][0]["text"]
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

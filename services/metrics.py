"""
Сервис комплексных метрик (TG Stat-подобная аналитика)
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func, case

from database import async_session_maker, Channel, Manager, Order, Client, PostAnalytics

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Текущее UTC-время как naive datetime (совместимо с полями DateTime в БД)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _period_bounds(period: str) -> tuple[datetime, datetime, datetime, datetime]:
    """Вернуть (start, end, prev_start, prev_end) для заданного периода."""
    now = _utcnow()
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
        prev_start = start - timedelta(days=1)
        prev_end = start
    elif period == "week":
        start = now - timedelta(days=7)
        end = now
        prev_start = start - timedelta(days=7)
        prev_end = start
    else:  # month
        start = now - timedelta(days=30)
        end = now
        prev_start = start - timedelta(days=30)
        prev_end = start
    return start, end, prev_start, prev_end


def _delta_str(current: float, previous: float) -> str:
    """Сформировать строку изменения с иконкой тренда."""
    if previous <= 0:
        return " ▲ новое" if current > 0 else ""
    change = (current - previous) / previous * 100
    arrow = "▲" if change >= 0 else "▼"
    return f" {arrow}{abs(change):.1f}%"


async def get_sales_metrics(period: str = "month") -> Optional[dict]:
    """
    Метрики продаж за выбранный период с трендами.

    Возвращает словарь с полями:
      period, revenue, revenue_prev, revenue_delta,
      orders, orders_prev, orders_delta,
      avg_order_value, conversion_rate, cancel_rate,
      new_clients, new_clients_prev, new_clients_delta
    """
    try:
        start, end, prev_start, prev_end = _period_bounds(period)
        async with async_session_maker() as session:
            # Выручка за текущий период
            revenue = float((await session.execute(
                select(func.sum(Order.final_price)).where(
                    Order.status == "payment_confirmed",
                    Order.paid_at >= start, Order.paid_at < end,
                )
            )).scalar() or 0)
            revenue_prev = float((await session.execute(
                select(func.sum(Order.final_price)).where(
                    Order.status == "payment_confirmed",
                    Order.paid_at >= prev_start, Order.paid_at < prev_end,
                )
            )).scalar() or 0)

            # Новые заказы (все статусы) за период
            orders = (await session.execute(
                select(func.count(Order.id)).where(
                    Order.created_at >= start, Order.created_at < end,
                )
            )).scalar() or 0
            orders_prev = (await session.execute(
                select(func.count(Order.id)).where(
                    Order.created_at >= prev_start, Order.created_at < prev_end,
                )
            )).scalar() or 0

            # Подтверждённые заказы в периоде (для конверсии)
            confirmed = (await session.execute(
                select(func.count(Order.id)).where(
                    Order.status == "payment_confirmed",
                    Order.created_at >= start, Order.created_at < end,
                )
            )).scalar() or 0
            cancelled = (await session.execute(
                select(func.count(Order.id)).where(
                    Order.status == "cancelled",
                    Order.created_at >= start, Order.created_at < end,
                )
            )).scalar() or 0

            # Новые клиенты
            new_clients = (await session.execute(
                select(func.count(Client.id)).where(
                    Client.created_at >= start, Client.created_at < end,
                )
            )).scalar() or 0
            new_clients_prev = (await session.execute(
                select(func.count(Client.id)).where(
                    Client.created_at >= prev_start, Client.created_at < prev_end,
                )
            )).scalar() or 0

        avg_order_value = revenue / confirmed if confirmed > 0 else 0
        conversion_rate = round(confirmed / orders * 100, 1) if orders > 0 else 0
        cancel_rate = round(cancelled / orders * 100, 1) if orders > 0 else 0

        return {
            "period": period,
            "revenue": revenue,
            "revenue_prev": revenue_prev,
            "revenue_delta": _delta_str(revenue, revenue_prev),
            "orders": orders,
            "orders_prev": orders_prev,
            "orders_delta": _delta_str(orders, orders_prev),
            "confirmed": confirmed,
            "avg_order_value": avg_order_value,
            "conversion_rate": conversion_rate,
            "cancel_rate": cancel_rate,
            "new_clients": new_clients,
            "new_clients_prev": new_clients_prev,
            "new_clients_delta": _delta_str(new_clients, new_clients_prev),
        }
    except Exception as e:
        logger.error(f"get_sales_metrics error: {e}")
        return None


async def get_channel_metrics() -> Optional[dict]:
    """
    Метрики по каналам: топ-5 по выручке,
    средний CPM и средний ERR% по активным каналам.

    Данные берутся из БД, которая заполняется ботом напрямую через
    Telegram Bot API (subscribers via get_chat_member_count,
    avg_reach / ERR из накопленных PostAnalytics).
    """
    try:
        from database.models import Slot
        async with async_session_maker() as session:
            # Топ каналов по выручке (Order -> Slot -> Channel)
            top_by_revenue = (await session.execute(
                select(
                    Channel.name,
                    func.sum(Order.final_price).label("rev"),
                    func.count(Order.id).label("cnt"),
                )
                .select_from(Order)
                .join(Slot, Order.slot_id == Slot.id)
                .join(Channel, Slot.channel_id == Channel.id)
                .where(Order.status == "payment_confirmed")
                .group_by(Channel.id, Channel.name)
                .order_by(func.sum(Order.final_price).desc())
                .limit(5)
            )).all()

            # Средний CPM по активным каналам
            avg_cpm = float((await session.execute(
                select(func.avg(Channel.cpm)).where(
                    Channel.is_active == True,
                    Channel.cpm > 0,
                )
            )).scalar() or 0)

            # Средний ERR из поля канала (рассчитывается коллектором из PostAnalytics)
            avg_err = float((await session.execute(
                select(func.avg(Channel.err_percent)).where(
                    Channel.is_active == True,
                    Channel.err_percent > 0,
                )
            )).scalar() or 0)

            # Средний охват из поля канала (рассчитывается коллектором из PostAnalytics)
            avg_reach = float((await session.execute(
                select(func.avg(Channel.avg_reach_24h)).where(
                    Channel.is_active == True,
                    Channel.avg_reach_24h > 0,
                )
            )).scalar() or 0)

            total_active = (await session.execute(
                select(func.count(Channel.id)).where(Channel.is_active == True)
            )).scalar() or 0

            # Суммарные просмотры по PostAnalytics (факт, собранные ботом)
            total_views = (await session.execute(
                select(func.sum(PostAnalytics.views)).where(PostAnalytics.views > 0)
            )).scalar() or 0

            analytics_posts = (await session.execute(
                select(func.count(PostAnalytics.id)).where(PostAnalytics.views > 0)
            )).scalar() or 0

        return {
            "top_by_revenue": [(r.name, float(r.rev or 0), r.cnt) for r in top_by_revenue],
            "avg_cpm": avg_cpm,
            "avg_err": avg_err,
            "avg_reach": round(avg_reach),
            "total_active": total_active,
            "total_views_tracked": int(total_views),
            "analytics_posts": analytics_posts,
        }
    except Exception as e:
        logger.error(f"get_channel_metrics error: {e}")
        return None


async def get_manager_metrics() -> Optional[dict]:
    """
    Метрики по менеджерам: топ-5 по выручке, по конверсии,
    средний чек менеджера.
    """
    try:
        async with async_session_maker() as session:
            # Топ по выручке (все подтверждённые заказы)
            top_rev = (await session.execute(
                select(
                    Manager.first_name,
                    Manager.username,
                    func.sum(Order.final_price).label("rev"),
                    func.count(Order.id).label("cnt"),
                )
                .join(Order, Order.manager_id == Manager.id)
                .where(Order.status == "payment_confirmed")
                .group_by(Manager.id, Manager.first_name, Manager.username)
                .order_by(func.sum(Order.final_price).desc())
                .limit(5)
            )).all()

            # Конверсия менеджеров (подтверждённые / все заказы)
            conv_rows = (await session.execute(
                select(
                    Manager.first_name,
                    Manager.username,
                    func.count(Order.id).label("total"),
                    func.sum(
                        case((Order.status == "payment_confirmed", 1), else_=0)
                    ).label("confirmed"),
                )
                .join(Order, Order.manager_id == Manager.id)
                .where(Manager.is_active == True)
                .group_by(Manager.id, Manager.first_name, Manager.username)
                .having(func.count(Order.id) >= 3)
                .order_by(
                    (func.sum(case((Order.status == "payment_confirmed", 1), else_=0)) /
                     func.count(Order.id)).desc()
                )
                .limit(5)
            )).all()

            # Средний чек по менеджерам
            avg_check = float((await session.execute(
                select(func.avg(Order.final_price)).where(
                    Order.status == "payment_confirmed",
                    Order.manager_id.is_not(None),
                )
            )).scalar() or 0)

        top_revenue = [
            {
                "name": r.first_name or r.username or "—",
                "revenue": float(r.rev),
                "orders": r.cnt,
            }
            for r in top_rev
        ]
        top_conversion = [
            {
                "name": r.first_name or r.username or "—",
                "total": r.total,
                "confirmed": r.confirmed,
                "rate": round(r.confirmed / r.total * 100, 1) if r.total > 0 else 0,
            }
            for r in conv_rows
        ]
        return {
            "top_revenue": top_revenue,
            "top_conversion": top_conversion,
            "avg_check": avg_check,
        }
    except Exception as e:
        logger.error(f"get_manager_metrics error: {e}")
        return None


async def get_client_metrics() -> Optional[dict]:
    """
    Метрики по клиентам: новые vs. повторные, средний LTV,
    топ-5 клиентов по тратам.
    """
    try:
        now = datetime.utcnow()
        month_ago = now - timedelta(days=30)
        async with async_session_maker() as session:
            total_clients = (await session.execute(
                select(func.count(Client.id))
            )).scalar() or 0

            new_clients = (await session.execute(
                select(func.count(Client.id)).where(Client.created_at >= month_ago)
            )).scalar() or 0

            # Клиенты с повторными заказами (2+)
            repeat_clients = (await session.execute(
                select(func.count()).select_from(
                    select(Client.id)
                    .where(Client.total_orders >= 2)
                    .subquery()
                )
            )).scalar() or 0

            avg_ltv = float((await session.execute(
                select(func.avg(Client.total_spent)).where(Client.total_spent > 0)
            )).scalar() or 0)

            avg_orders_per_client = float((await session.execute(
                select(func.avg(Client.total_orders)).where(Client.total_orders > 0)
            )).scalar() or 0)

            # Топ клиентов по тратам
            top_clients = (await session.execute(
                select(
                    Client.first_name,
                    Client.username,
                    Client.total_spent,
                    Client.total_orders,
                )
                .where(Client.total_spent > 0)
                .order_by(Client.total_spent.desc())
                .limit(5)
            )).all()

        return {
            "total": total_clients,
            "new_month": new_clients,
            "repeat": repeat_clients,
            "repeat_rate": round(repeat_clients / total_clients * 100, 1) if total_clients > 0 else 0,
            "avg_ltv": avg_ltv,
            "avg_orders_per_client": round(avg_orders_per_client, 1),
            "top": [
                {
                    "name": r.first_name or r.username or "—",
                    "spent": float(r.total_spent),
                    "orders": r.total_orders,
                }
                for r in top_clients
            ],
        }
    except Exception as e:
        logger.error(f"get_client_metrics error: {e}")
        return None


async def get_format_metrics() -> Optional[dict]:
    """
    Разбивка заказов по форматам размещения (1/24, 1/48, 2/48, native):
    количество, выручка, доля.
    """
    try:
        async with async_session_maker() as session:
            rows = (await session.execute(
                select(
                    Order.format_type,
                    func.count(Order.id).label("cnt"),
                    func.sum(Order.final_price).label("rev"),
                )
                .where(Order.status == "payment_confirmed")
                .group_by(Order.format_type)
                .order_by(func.sum(Order.final_price).desc())
            )).all()

            total_orders = sum(r.cnt for r in rows)
            total_revenue = sum(float(r.rev or 0) for r in rows)

        formats = [
            {
                "type": r.format_type or "—",
                "orders": r.cnt,
                "revenue": float(r.rev or 0),
                "order_share": round(r.cnt / total_orders * 100, 1) if total_orders > 0 else 0,
                "revenue_share": round(float(r.rev or 0) / total_revenue * 100, 1) if total_revenue > 0 else 0,
            }
            for r in rows
        ]
        return {"formats": formats, "total_orders": total_orders, "total_revenue": total_revenue}
    except Exception as e:
        logger.error(f"get_format_metrics error: {e}")
        return None


async def get_post_analytics_metrics() -> Optional[dict]:
    """
    Сводная аналитика рекламных постов:
    средние показатели просмотров, реакций, ER; топ-5 по ER.
    """
    try:
        async with async_session_maker() as session:
            count = (await session.execute(
                select(func.count(PostAnalytics.id))
            )).scalar() or 0

            if count == 0:
                return {"count": 0, "avg_views": 0, "avg_reactions": 0, "avg_er": 0, "top": []}

            avg_views = float((await session.execute(
                select(func.avg(PostAnalytics.views)).where(PostAnalytics.views > 0)
            )).scalar() or 0)

            avg_reactions = float((await session.execute(
                select(func.avg(PostAnalytics.reactions)).where(PostAnalytics.views > 0)
            )).scalar() or 0)

            # Топ-5 постов по ER (реакции+пересылки+сохранения+комментарии / просмотры)
            all_rows = (await session.execute(
                select(
                    PostAnalytics.id,
                    PostAnalytics.views,
                    PostAnalytics.reactions,
                    PostAnalytics.forwards,
                    PostAnalytics.saves,
                    PostAnalytics.comments,
                    Channel.name.label("channel_name"),
                )
                .join(Channel, PostAnalytics.channel_id == Channel.id, isouter=True)
                .where(PostAnalytics.views > 0)
                .order_by(PostAnalytics.recorded_at.desc())
                .limit(100)
            )).all()

        er_list = []
        total_er = 0.0
        er_count = 0
        for r in all_rows:
            engage = r.reactions + r.forwards + r.saves + r.comments
            er = round(engage / r.views * 100, 2) if r.views > 0 else 0
            total_er += er
            er_count += 1
            er_list.append({
                "id": r.id,
                "channel": r.channel_name or "—",
                "views": r.views,
                "er": er,
            })

        er_list.sort(key=lambda x: x["er"], reverse=True)
        avg_er = round(total_er / er_count, 2) if er_count > 0 else 0

        return {
            "count": count,
            "avg_views": round(avg_views),
            "avg_reactions": round(avg_reactions, 1),
            "avg_er": avg_er,
            "top": er_list[:5],
        }
    except Exception as e:
        logger.error(f"get_post_analytics_metrics error: {e}")
        return None

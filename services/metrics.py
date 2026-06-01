"""
Сервис комплексных метрик (TG Stat-подобная аналитика)
"""
import html as html_module
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, func, case
from sqlalchemy.exc import ProgrammingError, OperationalError

from database import async_session_maker, Channel, Manager, Order, Client, PostAnalytics
from database.models import ScheduledPost, PostViewSnapshot, ChannelSubscriberSnapshot

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    """Текущее UTC-время как naive datetime (совместимо с полями DateTime в БД)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _channel_avg_reach(channel) -> int:
    """Вернуть лучшее доступное значение среднего охвата для канала."""
    return int(channel.avg_reach_24h or channel.avg_reach or 0)


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


def _md_escape(text: str) -> str:
    text = str(text or "")
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def _format_money(value: Optional[float], decimals: int = 0) -> str:
    if value is None:
        return "—"
    if decimals <= 0:
        return f"{value:,.0f}₽"
    return f"{value:,.{decimals}f}₽"


def _format_signed(value: Optional[int]) -> str:
    if value is None:
        return "—"
    if value > 0:
        return f"+{value:,}"
    return f"{value:,}"


def _extract_creative_title(content: Optional[str], signature: Optional[str] = None, limit: int = 72) -> str:
    raw = content or signature or ""
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html_module.unescape(raw)
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    title = lines[0] if lines else "Без названия"
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


def _resolve_snapshot_subscribers(snapshots: list, anchor: datetime) -> Optional[int]:
    before = None
    after = None

    for snapshot in snapshots:
        recorded_at = getattr(snapshot, "recorded_at", None)
        if not recorded_at:
            continue
        if recorded_at <= anchor:
            before = snapshot
            continue
        after = snapshot
        break

    chosen = before or after
    if not chosen:
        return None
    return int(getattr(chosen, "subscribers", 0) or 0)


def _build_quick_stat_numbers(delta: Optional[int]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    if delta is None:
        return None, None, None
    subscribed = max(delta, 0)
    unsubscribed = abs(min(delta, 0))
    return subscribed, unsubscribed, delta


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
            top_by_revenue = []
            try:
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
            except Exception as e:
                logger.warning(f"get_channel_metrics: top_by_revenue query failed: {e}", exc_info=True)

            # Средний CPM по активным каналам
            avg_cpm = 0.0
            try:
                avg_cpm = float((await session.execute(
                    select(func.avg(Channel.cpm)).where(
                        Channel.is_active == True,
                        Channel.cpm > 0,
                    )
                )).scalar() or 0)
            except Exception as e:
                logger.warning(f"get_channel_metrics: avg_cpm query failed: {e}", exc_info=True)

            # Средний ERR из поля канала (рассчитывается коллектором из PostAnalytics)
            avg_err = 0.0
            try:
                avg_err = float((await session.execute(
                    select(func.avg(Channel.err_percent)).where(
                        Channel.is_active == True,
                        Channel.err_percent > 0,
                    )
                )).scalar() or 0)
            except Exception as e:
                logger.warning(f"get_channel_metrics: avg_err query failed: {e}", exc_info=True)

            # Средний охват из поля канала (рассчитывается коллектором из PostAnalytics)
            avg_reach = 0.0
            try:
                avg_reach = float((await session.execute(
                    select(func.avg(Channel.avg_reach_24h)).where(
                        Channel.is_active == True,
                        Channel.avg_reach_24h > 0,
                    )
                )).scalar() or 0)
            except Exception as e:
                logger.warning(f"get_channel_metrics: avg_reach query failed: {e}", exc_info=True)

            total_active = 0
            try:
                total_active = (await session.execute(
                    select(func.count(Channel.id)).where(Channel.is_active == True)
                )).scalar() or 0
            except Exception as e:
                logger.warning(f"get_channel_metrics: total_active query failed: {e}", exc_info=True)

            # Суммарные просмотры по PostAnalytics (факт, собранные ботом)
            total_views = 0
            try:
                total_views = (await session.execute(
                    select(func.sum(PostAnalytics.views)).where(PostAnalytics.views > 0)
                )).scalar() or 0
            except Exception as e:
                logger.warning(f"get_channel_metrics: total_views query failed: {e}", exc_info=True)

            analytics_posts = 0
            try:
                analytics_posts = (await session.execute(
                    select(func.count(PostAnalytics.id)).where(PostAnalytics.views > 0)
                )).scalar() or 0
            except Exception as e:
                logger.warning(f"get_channel_metrics: analytics_posts query failed: {e}", exc_info=True)

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
        logger.error(f"get_channel_metrics error: {e}", exc_info=True)
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
        now = _utcnow()
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
            # JOIN по channel_id может завершиться ошибкой, если колонка ещё не добавлена
            # миграцией — в этом случае возвращаем посты без названия канала.
            try:
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
                )).mappings().all()
                all_rows = [dict(r) for r in all_rows]
            except (ProgrammingError, OperationalError) as chan_join_e:
                logger.warning(
                    f"get_post_analytics_metrics: channel JOIN failed (falling back to no channel name): {chan_join_e}"
                )
                raw_rows = (await session.execute(
                    select(
                        PostAnalytics.id,
                        PostAnalytics.views,
                        PostAnalytics.reactions,
                        PostAnalytics.forwards,
                        PostAnalytics.saves,
                        PostAnalytics.comments,
                    )
                    .where(PostAnalytics.views > 0)
                    .order_by(PostAnalytics.recorded_at.desc())
                    .limit(100)
                )).mappings().all()
                all_rows = [dict(r, channel_name=None) for r in raw_rows]

        er_list = []
        total_er = 0.0
        er_count = 0
        for r in all_rows:
            engage = r["reactions"] + r["forwards"] + r["saves"] + r["comments"]
            er = round(engage / r["views"] * 100, 2) if r["views"] > 0 else 0
            total_er += er
            er_count += 1
            er_list.append({
                "id": r["id"],
                "channel": r.get("channel_name") or "—",
                "views": r["views"],
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


async def get_channel_analytics_detail(channel_id: int) -> Optional[dict]:
    """
    Детальная аналитика для одного канала:
    базовые поля из Channel + данные из PostAnalytics, собранных ботом.

    Возвращает словарь с ключами:
      channel, posts_count, posts_with_views, total_views,
      avg_views, avg_er, recent_posts, analytics_unavailable (опционально)

    Возвращает None только если канал не найден или критическая ошибка БД.
    """
    try:
        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                return None

            ch_snapshot = {
                "id": channel.id,
                "name": channel.name,
                "username": channel.username,
                "subscribers": channel.subscribers or 0,
                "avg_reach": _channel_avg_reach(channel),
                "err_percent": float(channel.err_percent or 0),
                "analytics_updated": channel.analytics_updated,
                "telemetr_id": channel.telemetr_id,
            }

            try:
                posts_count = (await session.execute(
                    select(func.count(PostAnalytics.id))
                    .where(PostAnalytics.channel_id == channel_id)
                )).scalar() or 0

                posts_with_views = (await session.execute(
                    select(func.count(PostAnalytics.id))
                    .where(PostAnalytics.channel_id == channel_id, PostAnalytics.views > 0)
                )).scalar() or 0

                total_views = int((await session.execute(
                    select(func.sum(PostAnalytics.views))
                    .where(PostAnalytics.channel_id == channel_id, PostAnalytics.views > 0)
                )).scalar() or 0)

                avg_views_raw = float((await session.execute(
                    select(func.avg(PostAnalytics.views))
                    .where(PostAnalytics.channel_id == channel_id, PostAnalytics.views > 0)
                )).scalar() or 0)

                recent_rows = (await session.execute(
                    select(PostAnalytics)
                    .where(PostAnalytics.channel_id == channel_id, PostAnalytics.views > 0)
                    .order_by(PostAnalytics.recorded_at.desc())
                    .limit(5)
                )).scalars().all()

                # Build recent_posts inside session while ORM objects are still live
                recent_posts = []
                total_er = 0.0
                er_count = 0
                for r in recent_rows:
                    views = r.views or 0
                    engage = (r.reactions or 0) + (r.forwards or 0) + (r.saves or 0) + (r.comments or 0)
                    er = round(engage / views * 100, 2) if views > 0 else 0
                    total_er += er
                    er_count += 1
                    recent_posts.append({
                        "id": r.id,
                        "views": views,
                        "reactions": r.reactions or 0,
                        "forwards": r.forwards or 0,
                        "er": er,
                        "recorded_at": r.recorded_at,
                    })

                avg_er = round(total_er / er_count, 2) if er_count > 0 else 0

                return {
                    "channel": ch_snapshot,
                    "posts_count": posts_count,
                    "posts_with_views": posts_with_views,
                    "total_views": total_views,
                    "avg_views": round(avg_views_raw),
                    "avg_er": avg_er,
                    "recent_posts": recent_posts,
                }
            except Exception as analytics_e:
                logger.error(
                    f"get_channel_analytics_detail PostAnalytics error for channel {channel_id}: {analytics_e}",
                    exc_info=True,
                )
                return {
                    "channel": ch_snapshot,
                    "posts_count": 0,
                    "posts_with_views": 0,
                    "total_views": 0,
                    "avg_views": 0,
                    "avg_er": 0,
                    "recent_posts": [],
                    "analytics_unavailable": True,
                }
    except Exception as e:
        logger.error(f"get_channel_analytics_detail error: {e}", exc_info=True)
        return None


async def get_channels_analytics_summary() -> Optional[list]:
    """
    Краткая сводка по всем активным каналам для списка аналитики.

    Возвращает список словарей:
      id, name, subscribers, avg_reach, err_percent,
      posts_count, total_views

    Возвращает None при ошибке обращения к БД.
    """
    try:
        async with async_session_maker() as session:
            # Получаем активные каналы; если колонка is_active ещё не добавлена — возвращаем все каналы
            try:
                channels = (await session.execute(
                    select(Channel).where(Channel.is_active.is_not(False)).order_by(Channel.name)
                )).scalars().all()
            except (ProgrammingError, OperationalError):
                channels = (await session.execute(
                    select(Channel).order_by(Channel.name)
                )).scalars().all()

            # Количество PostAnalytics и суммарные просмотры по каналам.
            # Запрос оборачивается в try/except: если колонка channel_id ещё не добавлена
            # миграцией (или содержит только NULL-значения), возвращаем пустой словарь
            # и не падаем с ошибкой — каналы всё равно отображаются с нулевыми счётчиками.
            posts_by_channel: dict = {}
            try:
                analytics_rows = (await session.execute(
                    select(
                        PostAnalytics.channel_id,
                        func.count(PostAnalytics.id).label("cnt"),
                        func.sum(PostAnalytics.views).label("total_views"),
                    )
                    .where(PostAnalytics.channel_id.is_not(None))
                    .group_by(PostAnalytics.channel_id)
                )).all()
                posts_by_channel = {r.channel_id: (r.cnt, int(r.total_views or 0)) for r in analytics_rows}
            except (ProgrammingError, OperationalError) as analytics_e:
                logger.warning(
                    f"get_channels_analytics_summary: PostAnalytics query failed, "
                    f"returning zero counts: {analytics_e}"
                )

            result = []
            for ch in channels:
                cnt, total_v = posts_by_channel.get(ch.id, (0, 0))
                result.append({
                    "id": ch.id,
                    "name": ch.name,
                    "username": ch.username,
                    "subscribers": ch.subscribers or 0,
                    "avg_reach": _channel_avg_reach(ch),
                    "err_percent": float(ch.err_percent or 0),
                    "posts_count": cnt,
                    "total_views": total_v,
                })
        return result
    except Exception as e:
        logger.error(f"get_channels_analytics_summary error: {e}", exc_info=True)
        return None


async def get_daily_reach_report() -> Optional[dict]:
    """
    Отчёт об охватах рекламных постов за последние 24 часа.

    Для каждого поста, опубликованного за последние 24 часа, возвращает:
    канал, подписчиков, текущие просмотры, просмотры за 24ч, ERR 24ч.

    Возвращает словарь с ключами:
      posts, count, total_views, total_views_24h, avg_err24
    """
    try:
        now = _utcnow()
        since_24h = now - timedelta(hours=24)

        async with async_session_maker() as session:
            rows = (await session.execute(
                select(
                    ScheduledPost.id,
                    ScheduledPost.posted_at,
                    ScheduledPost.channel_id,
                    PostAnalytics.views,
                    PostAnalytics.reactions,
                    PostAnalytics.forwards,
                    PostAnalytics.saves,
                    PostAnalytics.comments,
                    Channel.name.label("channel_name"),
                    Channel.subscribers,
                )
                .join(PostAnalytics, PostAnalytics.scheduled_post_id == ScheduledPost.id, isouter=True)
                .join(Channel, Channel.id == ScheduledPost.channel_id, isouter=True)
                .where(
                    ScheduledPost.posted_at >= since_24h,
                    ScheduledPost.status.in_(["posted", "deleted"]),
                )
                .order_by(ScheduledPost.posted_at.desc())
            )).all()

            if not rows:
                return {"posts": [], "count": 0, "total_views": 0, "total_views_24h": 0, "avg_err24": 0.0}

            post_ids = [r.id for r in rows]

            snapshots_all = (await session.execute(
                select(PostViewSnapshot)
                .where(PostViewSnapshot.scheduled_post_id.in_(post_ids))
                .order_by(PostViewSnapshot.recorded_at.asc())
            )).scalars().all()

        # Группируем снимки по scheduled_post_id
        snapshots_by_post: dict = {}
        for s in snapshots_all:
            snapshots_by_post.setdefault(s.scheduled_post_id, []).append(s)

        results = []
        total_views = 0
        total_views_24h = 0
        err24_sum = 0.0
        err24_count = 0

        for r in rows:
            views = r.views or 0
            reactions = r.reactions or 0
            forwards = r.forwards or 0
            saves = r.saves or 0
            comments = r.comments or 0
            total_engage = reactions + forwards + saves + comments

            # Просмотры за 24 часа из снимков
            post_snaps = snapshots_by_post.get(r.id, [])
            views_24h = views  # fallback: текущие просмотры
            if post_snaps and r.posted_at:
                cutoff_24h = r.posted_at + timedelta(hours=24)
                snaps_24h = [s for s in post_snaps if s.recorded_at <= cutoff_24h]
                if snaps_24h:
                    views_24h = snaps_24h[-1].views

            err24 = round(total_engage / views_24h * 100, 1) if views_24h > 0 else 0.0

            total_views += views
            total_views_24h += views_24h
            if views_24h > 0:
                err24_sum += err24
                err24_count += 1

            results.append({
                "post_id": r.id,
                "channel_name": r.channel_name or "—",
                "subscribers": r.subscribers or 0,
                "views": views,
                "views_24h": views_24h,
                "err24": err24,
                "posted_at": r.posted_at,
            })

        avg_err24 = round(err24_sum / err24_count, 1) if err24_count > 0 else 0.0

        return {
            "posts": results,
            "count": len(results),
            "total_views": total_views,
            "total_views_24h": total_views_24h,
            "avg_err24": avg_err24,
        }
    except Exception as e:
        logger.error(f"get_daily_reach_report error: {e}", exc_info=True)
        return None


async def get_channel_quick_stats(channel_id: int, period_hours: int = 48) -> Optional[dict]:
    """Быстрая статистика размещений по каналу за последние N часов."""
    try:
        hours = max(24, min(168, int(period_hours or 48)))
        now = _utcnow()
        period_start = now - timedelta(hours=hours)
        snapshot_start = period_start - timedelta(hours=24)

        async with async_session_maker() as session:
            channel = await session.get(Channel, channel_id)
            if not channel:
                return None

            post_rows = (
                await session.execute(
                    select(
                        ScheduledPost.id.label("post_id"),
                        ScheduledPost.posted_at,
                        ScheduledPost.content,
                        ScheduledPost.signature,
                        ScheduledPost.price,
                        ScheduledPost.order_id,
                        ScheduledPost.created_by,
                        func.max(PostAnalytics.views).label("views"),
                        Order.final_price.label("order_price"),
                        Manager.first_name.label("order_manager_name"),
                    )
                    .select_from(ScheduledPost)
                    .outerjoin(PostAnalytics, PostAnalytics.scheduled_post_id == ScheduledPost.id)
                    .outerjoin(Order, Order.id == ScheduledPost.order_id)
                    .outerjoin(Manager, Manager.id == Order.manager_id)
                    .where(
                        ScheduledPost.channel_id == channel_id,
                        ScheduledPost.posted_at.is_not(None),
                        ScheduledPost.posted_at >= period_start,
                        ScheduledPost.posted_at <= now,
                    )
                    .group_by(
                        ScheduledPost.id,
                        ScheduledPost.posted_at,
                        ScheduledPost.content,
                        ScheduledPost.signature,
                        ScheduledPost.price,
                        ScheduledPost.order_id,
                        ScheduledPost.created_by,
                        Order.final_price,
                        Manager.first_name,
                    )
                    .order_by(ScheduledPost.posted_at.desc(), ScheduledPost.id.desc())
                )
            ).all()

            created_by_ids = {row.created_by for row in post_rows if row.created_by}
            creator_names = {}
            if created_by_ids:
                creator_rows = (
                    await session.execute(
                        select(Manager.telegram_id, Manager.first_name).where(Manager.telegram_id.in_(created_by_ids))
                    )
                ).all()
                creator_names = {
                    row.telegram_id: row.first_name
                    for row in creator_rows
                    if row.telegram_id is not None and row.first_name
                }

            older_snapshot = (
                await session.execute(
                    select(ChannelSubscriberSnapshot)
                    .where(
                        ChannelSubscriberSnapshot.channel_id == channel_id,
                        ChannelSubscriberSnapshot.recorded_at < snapshot_start,
                    )
                    .order_by(ChannelSubscriberSnapshot.recorded_at.desc())
                    .limit(1)
                )
            ).scalars().first()
            recent_snapshots = (
                await session.execute(
                    select(ChannelSubscriberSnapshot)
                    .where(
                        ChannelSubscriberSnapshot.channel_id == channel_id,
                        ChannelSubscriberSnapshot.recorded_at >= snapshot_start,
                    )
                    .order_by(ChannelSubscriberSnapshot.recorded_at.asc())
                )
            ).scalars().all()

        snapshots = ([older_snapshot] if older_snapshot else []) + list(recent_snapshots)
        current_subscribers = int(snapshots[-1].subscribers) if snapshots else int(channel.subscribers or 0)

        posts = []
        total_views = 0
        total_cost = 0.0
        total_subscribed = 0
        total_unsubscribed = 0
        tracked_deltas = 0

        for row in post_rows:
            posted_at = row.posted_at
            raw_cost = row.price if row.price is not None else row.order_price
            cost = float(raw_cost) if raw_cost is not None else None
            views = int(row.views or 0)
            baseline_subscribers = _resolve_snapshot_subscribers(snapshots, posted_at) if posted_at else None
            subscriber_delta = (
                int(current_subscribers - baseline_subscribers)
                if baseline_subscribers is not None
                else None
            )
            subscribed, unsubscribed, current_left = _build_quick_stat_numbers(subscriber_delta)

            if subscriber_delta is not None:
                tracked_deltas += 1
                total_subscribed += subscribed or 0
                total_unsubscribed += unsubscribed or 0

            total_views += views
            if cost is not None:
                total_cost += cost

            cpm = round(cost * 1000 / views, 2) if cost is not None and views > 0 else None
            cost_per_view = round(cost / views, 2) if cost is not None and views > 0 else None

            posts.append({
                "post_id": row.post_id,
                "posted_at": posted_at,
                "creative": _extract_creative_title(row.content, row.signature),
                "manager_name": row.order_manager_name or creator_names.get(row.created_by) or "—",
                "views": views,
                "cost": cost,
                "cpm": cpm,
                "cost_per_view": cost_per_view,
                "baseline_subscribers": baseline_subscribers,
                "current_subscribers": current_subscribers,
                "subscribed": subscribed,
                "unsubscribed": unsubscribed,
                "current_left": current_left,
            })

        total_current_left = total_subscribed - total_unsubscribed if tracked_deltas else None
        avg_cpm = round(total_cost * 1000 / total_views, 2) if total_views > 0 else None

        return {
            "channel": {
                "id": channel.id,
                "name": channel.name,
                "username": channel.username,
                "subscribers": int(channel.subscribers or 0),
                "current_subscribers": current_subscribers,
                "analytics_updated": channel.analytics_updated,
            },
            "period_hours": hours,
            "generated_at": now,
            "posts": posts,
            "posts_count": len(posts),
            "total_views": total_views,
            "total_cost": round(total_cost, 2),
            "avg_cpm": avg_cpm,
            "total_subscribed": total_subscribed if tracked_deltas else None,
            "total_unsubscribed": total_unsubscribed if tracked_deltas else None,
            "total_current_left": total_current_left,
            "tracked_deltas": tracked_deltas,
            "has_snapshot_history": bool(snapshots),
        }
    except Exception as e:
        logger.error(f"get_channel_quick_stats error: {e}", exc_info=True)
        return None


def format_channel_quick_stats_text(data: dict, bold: str = "**") -> str:
    """Форматировать быструю статистику канала."""
    b = bold
    channel = data["channel"]
    posts = data.get("posts", [])
    generated_at = data.get("generated_at")
    updated_at = generated_at.strftime("%d.%m.%Y %H:%M") if generated_at else "—"

    text = (
        f"⚡ {b}Быстрая статистика канала{b}\n"
        f"📢 {_md_escape(channel.get('name') or '—')}\n"
        f"🕒 Период: последние {data.get('period_hours', 48)} ч.\n"
        f"🔄 Актуально на: {updated_at}\n\n"
        f"👥 Подписчиков сейчас: {b}{channel.get('current_subscribers', 0):,}{b}\n"
        f"📝 Размещений: {b}{data.get('posts_count', 0)}{b}\n"
        f"👁 Просмотров: {b}{data.get('total_views', 0):,}{b}\n"
        f"💰 Расход: {b}{_format_money(data.get('total_cost'))}{b}\n"
        f"📊 CPM: {b}{_format_money(data.get('avg_cpm'))}{b}\n"
    )

    if data.get("tracked_deltas"):
        text += (
            f"📈 Подписалось ≈ {b}{_format_signed(data.get('total_subscribed'))}{b}\n"
            f"📉 Отписалось ≈ {b}{_format_signed(data.get('total_unsubscribed'))}{b}\n"
            f"👤 Осталось сейчас: {b}{_format_signed(data.get('total_current_left'))}{b}\n"
        )
    else:
        text += "📈 Дельта подписчиков: _появится после накопления истории._\n"

    text += "\nℹ️ _Подписки и отписки рассчитываются приблизительно по снимкам аудитории канала._\n"

    if not posts:
        text += "\n_За выбранный период размещений нет._"
        return text

    current_day = None
    for post in posts:
        posted_at = post.get("posted_at")
        post_day = posted_at.date() if posted_at else None
        if post_day != current_day:
            if current_day is not None:
                text += "\n"
            day_title = posted_at.strftime("%d.%m.%Y") if posted_at else "Без даты"
            text += f"\n📅 {b}{day_title}{b}\n"
            current_day = post_day

        posted_str = posted_at.strftime("%H:%M") if posted_at else "—"
        text += (
            f"\n🧾 {_md_escape(post.get('creative') or 'Без названия')}\n"
            f"🕒 Выход: {b}{posted_str}{b}\n"
            f"👤 Менеджер: {_md_escape(post.get('manager_name') or '—')}\n"
            f"👁 Просмотры: {b}{post.get('views', 0):,}{b}\n"
            f"💰 Стоимость: {b}{_format_money(post.get('cost'))}{b}\n"
            f"📊 CPM: {b}{_format_money(post.get('cpm'))}{b}\n"
            f"💵 Цена просмотра: {b}{_format_money(post.get('cost_per_view'), decimals=2)}{b}\n"
        )

        if post.get("current_left") is not None:
            text += (
                f"📈 Подписалось ≈ {b}{_format_signed(post.get('subscribed'))}{b}\n"
                f"📉 Отписалось ≈ {b}{_format_signed(post.get('unsubscribed'))}{b}\n"
                f"👤 Осталось сейчас: {b}{_format_signed(post.get('current_left'))}{b}\n"
            )
        else:
            text += "📈 Дельта подписчиков: _недостаточно истории._\n"

    return text


def format_daily_reach_report_text(data: dict, date_str: str, bold: str = "**") -> str:
    """
    Форматирует текст отчёта об охватах за сутки.

    Параметры:
      data     — результат get_daily_reach_report()
      date_str — строка даты для заголовка
      bold     — маркер жирного текста (``**`` для Markdown v1/v2, ``*`` для MarkdownV1)

    ERR 24ч рассчитывается как (все реакции+пересылки+сохранения+комментарии) /
    просмотры за первые 24 часа × 100 %.  Текущие данные реакций используются в
    качестве приближения, поскольку бот не хранит отдельные метрики вовлечённости
    в разрезе временных окон.
    """
    b = bold
    posts = data.get("posts", [])

    text = f"👁 {b}Охваты рекламных постов за сутки{b} ({date_str})\n\n"

    if not posts:
        text += "_За последние 24 часа рекламных постов не публиковалось._"
        return text

    text += (
        f"📝 Постов: {b}{data['count']}{b}\n"
        f"👁 Текущие просмотры (итого): {b}{data['total_views']:,}{b}\n"
        f"📈 Просмотры за 24ч (итого): {b}{data['total_views_24h']:,}{b}\n"
        f"❗ Средний ERR 24ч: {b}{data['avg_err24']}%{b}\n\n"
    )
    for p in posts:
        posted_str = p["posted_at"].strftime("%d.%m %H:%M") if p["posted_at"] else "—"
        channel = p["channel_name"].replace("*", "\\*").replace("_", "\\_")
        text += (
            f"📢 {channel}\n"
            f"   📅 {posted_str} | 👥 {p['subscribers']:,} подп.\n"
            f"   👁 Сейчас: {b}{p['views']:,}{b} | 24ч: {b}{p['views_24h']:,}{b}"
            f" | ERR: {b}{p['err24']}%{b}\n\n"
        )
    return text

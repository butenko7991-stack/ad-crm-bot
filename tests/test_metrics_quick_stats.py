from datetime import datetime
from types import SimpleNamespace

from services.metrics import (
    _extract_creative_title,
    _resolve_snapshot_subscribers,
    format_channel_quick_stats_text,
    format_daily_reach_report_compact_text,
)


def test_extract_creative_title_strips_html_and_truncates():
    title = _extract_creative_title(
        "<b>Новый</b> оффер и длинное описание для обрезки\n\nПодробности внутри",
        limit=18,
    )

    assert title == "Новый оффер и дли…"


def test_resolve_snapshot_subscribers_prefers_latest_before_anchor():
    snapshots = [
        SimpleNamespace(recorded_at=datetime(2026, 6, 1, 9, 0), subscribers=100),
        SimpleNamespace(recorded_at=datetime(2026, 6, 1, 10, 0), subscribers=120),
        SimpleNamespace(recorded_at=datetime(2026, 6, 1, 12, 0), subscribers=150),
    ]

    assert _resolve_snapshot_subscribers(snapshots, datetime(2026, 6, 1, 10, 30)) == 120
    assert _resolve_snapshot_subscribers(snapshots, datetime(2026, 6, 1, 8, 30)) == 100


def test_format_channel_quick_stats_text_contains_summary_and_post_metrics():
    data = {
        "channel": {
            "name": "Test_Channel",
            "current_subscribers": 1500,
        },
        "period_hours": 48,
        "generated_at": datetime(2026, 6, 1, 12, 30),
        "posts_count": 1,
        "total_views": 2300,
        "total_cost": 5000.0,
        "avg_cpm": 2173.91,
        "tracked_deltas": 1,
        "total_subscribed": 120,
        "total_unsubscribed": 0,
        "total_current_left": 120,
        "posts": [{
            "posted_at": datetime(2026, 6, 1, 9, 15),
            "creative": "Новый оффер",
            "manager_name": "Иван_1",
            "views": 2300,
            "cost": 5000.0,
            "cpm": 2173.91,
            "cost_per_view": 2.17,
            "subscribed": 120,
            "unsubscribed": 0,
            "current_left": 120,
        }],
    }

    text = format_channel_quick_stats_text(data)

    assert "Быстрая статистика канала" in text
    assert "Test\\_Channel" in text
    assert "Подписалось ≈ **+120**" in text
    assert "Цена просмотра" in text
    assert "Иван\\_1" in text


def test_format_daily_reach_report_compact_text_with_posts():
    data = {
        "count": 2,
        "total_views_24h": 3200,
        "avg_err24": 1.8,
        "posts": [
            {
                "channel_name": "Канал_1",
                "views": 1000,
                "views_24h": 1200,
                "err24": 1.5,
                "posted_at": datetime(2026, 6, 1, 9, 15),
            },
            {
                "channel_name": "Канал_2",
                "views": 1700,
                "views_24h": 2000,
                "err24": 2.1,
                "posted_at": datetime(2026, 6, 1, 11, 45),
            },
        ],
    }

    text = format_daily_reach_report_compact_text(data, "01.06.2026")

    assert "Быстрый отчёт по автопостингу" in text
    assert "📝 Постов: **2**" in text
    assert "Канал\\_2" in text
    assert "👁 **2,000**" in text


def test_format_daily_reach_report_compact_text_without_posts():
    text = format_daily_reach_report_compact_text({"posts": []}, "01.06.2026")

    assert "Быстрый отчёт по автопостингу" in text
    assert "не было" in text

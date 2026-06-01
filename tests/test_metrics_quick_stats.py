from datetime import datetime
from types import SimpleNamespace

from services.metrics import (
    _extract_creative_title,
    _resolve_snapshot_subscribers,
    format_channel_quick_stats_text,
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

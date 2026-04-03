"""
Unit tests for utils/helpers.py

Covers pure helper functions that have no external dependencies:
  - utc_now / msk_now / to_utc / to_msk
  - _apply_err_adjustment
  - calculate_recommended_price
  - format_number / format_price
  - get_status_emoji
  - escape_md
  - truncate_text
  - channel_link
  - format_channel_stats_for_group
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from utils.helpers import (
    utc_now,
    msk_now,
    to_utc,
    to_msk,
    _apply_err_adjustment,
    calculate_recommended_price,
    format_number,
    format_price,
    get_status_emoji,
    escape_md,
    truncate_text,
    channel_link,
    format_channel_stats_for_group,
)
from config import MSK_OFFSET


# ─── Time helpers ─────────────────────────────────────────────────────────────

class TestUtcNow:
    def test_returns_naive_datetime(self):
        dt = utc_now()
        assert isinstance(dt, datetime)
        assert dt.tzinfo is None

    def test_is_close_to_real_time(self):
        import time
        from datetime import timezone
        before = datetime.now(timezone.utc).replace(tzinfo=None)
        result = utc_now()
        after = datetime.now(timezone.utc).replace(tzinfo=None)
        assert before <= result <= after + timedelta(seconds=1)


class TestMskNow:
    def test_returns_naive_datetime(self):
        dt = msk_now()
        assert isinstance(dt, datetime)
        assert dt.tzinfo is None

    def test_offset_from_utc(self):
        utc = utc_now()
        msk = msk_now()
        diff = msk - utc
        # Should differ by MSK_OFFSET (within a small tolerance)
        assert abs(diff - MSK_OFFSET) < timedelta(seconds=1)


class TestToUtcAndToMsk:
    def test_to_utc_reverses_msk_offset(self):
        msk_dt = datetime(2024, 6, 15, 12, 0, 0)
        utc_dt = to_utc(msk_dt)
        assert utc_dt == msk_dt - MSK_OFFSET

    def test_to_msk_applies_offset(self):
        utc_dt = datetime(2024, 6, 15, 9, 0, 0)
        msk_dt = to_msk(utc_dt)
        assert msk_dt == utc_dt + MSK_OFFSET

    def test_round_trip_msk_utc(self):
        original = datetime(2024, 3, 21, 18, 30, 0)
        assert to_msk(to_utc(original)) == original

    def test_round_trip_utc_msk(self):
        original = datetime(2024, 3, 21, 15, 30, 0)
        assert to_utc(to_msk(original)) == original


# ─── ERR adjustment ───────────────────────────────────────────────────────────

class TestApplyErrAdjustment:
    def test_no_adjustment_when_err_low(self):
        assert _apply_err_adjustment(1000.0, 10.0) == 1000.0

    def test_no_adjustment_at_exactly_15(self):
        # boundary: > 15 triggers 1.1x, == 15 does not
        assert _apply_err_adjustment(1000.0, 15.0) == 1000.0

    def test_1_1_multiplier_between_15_and_20(self):
        result = _apply_err_adjustment(1000.0, 16.0)
        assert result == pytest.approx(1100.0)

    def test_1_2_multiplier_above_20(self):
        result = _apply_err_adjustment(1000.0, 21.0)
        assert result == pytest.approx(1200.0)

    def test_exactly_20_gives_1_1_not_1_2(self):
        # boundary: > 20 triggers 1.2x, == 20 does not
        result = _apply_err_adjustment(1000.0, 20.0)
        assert result == pytest.approx(1100.0)

    def test_zero_err_no_adjustment(self):
        assert _apply_err_adjustment(500.0, 0.0) == 500.0


# ─── Price calculation ────────────────────────────────────────────────────────

class TestCalculateRecommendedPrice:
    def test_basic_1_24_formula(self):
        # price = (avg_reach * cpm) / 1000
        # humor cpm = 865, no err adjustment
        price = calculate_recommended_price(
            avg_reach=1000,
            category="humor",
            err_percent=0,
            format_type="1/24",
        )
        assert price == 865  # (1000 * 865) / 1000 = 865

    def test_cpm_override_takes_priority(self):
        price = calculate_recommended_price(
            avg_reach=1000,
            category="humor",
            err_percent=0,
            format_type="1/24",
            cpm_override=2000,
        )
        assert price == 2000  # (1000 * 2000) / 1000

    def test_format_1_48_uses_48h_reach(self):
        price = calculate_recommended_price(
            avg_reach=1000,
            category="humor",
            err_percent=0,
            format_type="1/48",
            cpm_override=1000,
            avg_reach_48h=2000,
        )
        assert price == 2000  # (2000 * 1000) / 1000

    def test_format_1_48_fallback_1_5x_when_no_48h(self):
        price = calculate_recommended_price(
            avg_reach=1000,
            category="humor",
            err_percent=0,
            format_type="1/48",
            cpm_override=1000,
            avg_reach_48h=0,
        )
        assert price == 1500  # 1000 * 1.5

    def test_format_2_48_is_2x(self):
        price = calculate_recommended_price(
            avg_reach=1000,
            category="humor",
            err_percent=0,
            format_type="2/48",
            cpm_override=1000,
        )
        assert price == 2000  # 1000 * 2.0

    def test_format_native_is_2_5x(self):
        price = calculate_recommended_price(
            avg_reach=1000,
            category="humor",
            err_percent=0,
            format_type="native",
            cpm_override=1000,
        )
        assert price == 2500  # 1000 * 2.5

    def test_err_adjustment_applied_to_1_24(self):
        # err > 20 → * 1.2
        price = calculate_recommended_price(
            avg_reach=1000,
            category="humor",
            err_percent=25,
            format_type="1/24",
            cpm_override=1000,
        )
        assert price == 1200  # 1000 * 1.2

    def test_unknown_category_fallback_cpm_1000(self):
        price = calculate_recommended_price(
            avg_reach=1000,
            category="nonexistent_cat",
            err_percent=0,
            format_type="1/24",
        )
        assert price == 1000  # fallback cpm=1000

    def test_zero_reach_gives_zero_price(self):
        price = calculate_recommended_price(
            avg_reach=0,
            category="humor",
            err_percent=0,
            format_type="1/24",
        )
        assert price == 0

    def test_returns_integer(self):
        price = calculate_recommended_price(
            avg_reach=123,
            category="humor",
            err_percent=0,
            format_type="1/24",
        )
        assert isinstance(price, int)


# ─── Formatting helpers ───────────────────────────────────────────────────────

class TestFormatNumber:
    def test_integer_no_decimals(self):
        assert format_number(1000.0) == "1 000"

    def test_large_number(self):
        assert format_number(1_000_000.0) == "1 000 000"

    def test_zero(self):
        assert format_number(0) == "0"

    def test_rounds_down(self):
        assert format_number(1234.9) == "1 235"


class TestFormatPrice:
    def test_adds_ruble_symbol(self):
        result = format_price(500.0)
        assert "₽" in result
        assert "500" in result

    def test_large_price(self):
        result = format_price(10000.0)
        assert "10 000₽" == result

    def test_zero_price(self):
        assert "0₽" == format_price(0)


# ─── Status emoji ─────────────────────────────────────────────────────────────

class TestGetStatusEmoji:
    @pytest.mark.parametrize("status,expected", [
        ("pending", "⏳"),
        ("payment_uploaded", "📤"),
        ("payment_confirmed", "✅"),
        ("posted", "📝"),
        ("completed", "✔️"),
        ("cancelled", "❌"),
        ("moderation", "🔍"),
        ("approved", "✅"),
        ("rejected", "❌"),
    ])
    def test_known_statuses(self, status, expected):
        assert get_status_emoji(status) == expected

    def test_unknown_status_returns_question_mark(self):
        assert get_status_emoji("nonexistent") == "❓"

    def test_empty_string_returns_question_mark(self):
        assert get_status_emoji("") == "❓"


# ─── escape_md ────────────────────────────────────────────────────────────────

class TestEscapeMd:
    def test_escapes_underscore(self):
        assert escape_md("hello_world") == "hello\\_world"

    def test_escapes_asterisk(self):
        assert escape_md("**bold**") == "\\*\\*bold\\*\\*"

    def test_escapes_backtick(self):
        assert escape_md("`code`") == "\\`code\\`"

    def test_escapes_square_brackets(self):
        assert escape_md("[link]") == "\\[link\\]"

    def test_plain_text_unchanged(self):
        assert escape_md("hello world") == "hello world"

    def test_none_returns_empty_string(self):
        assert escape_md(None) == ""

    def test_empty_string_returns_empty(self):
        assert escape_md("") == ""

    def test_multiple_specials(self):
        result = escape_md("_*`[]")
        assert result == "\\_\\*\\`\\[\\]"


# ─── truncate_text ────────────────────────────────────────────────────────────

class TestTruncateText:
    def test_short_text_unchanged(self):
        assert truncate_text("hello", 10) == "hello"

    def test_exact_max_length_unchanged(self):
        text = "a" * 100
        assert truncate_text(text, 100) == text

    def test_long_text_truncated_with_ellipsis(self):
        text = "a" * 110
        result = truncate_text(text, 100)
        assert result.endswith("...")
        assert len(result) == 100

    def test_default_max_length_100(self):
        text = "x" * 200
        result = truncate_text(text)
        assert len(result) == 100

    def test_truncation_preserves_content_start(self):
        text = "hello" + "x" * 200
        result = truncate_text(text, 10)
        assert result.startswith("hello")


# ─── channel_link ─────────────────────────────────────────────────────────────

class TestChannelLink:
    def test_link_with_username(self):
        result = channel_link("My Channel", "mychannel")
        assert result == "[My Channel](https://t.me/mychannel)"

    def test_no_username_returns_escaped_name(self):
        result = channel_link("My Channel", None)
        assert result == "My Channel"

    def test_empty_username_returns_name(self):
        result = channel_link("My Channel", "")
        assert result == "My Channel"

    def test_dash_username_returns_name(self):
        result = channel_link("My Channel", "—")
        assert result == "My Channel"

    def test_empty_name_returns_dash(self):
        result = channel_link("", "mychannel")
        assert result == "—"

    def test_none_name_returns_dash(self):
        result = channel_link(None, "mychannel")
        assert result == "—"

    def test_name_with_brackets_escaped(self):
        # channel_link only escapes ']' in the link text (to avoid breaking Markdown link syntax)
        result = channel_link("Ch[1]", "ch1")
        assert result == "[Ch[1\\]](https://t.me/ch1)"

    def test_name_with_markdown_special_chars_in_link(self):
        # underscore in name: in a link, only ] is escaped; _ is not escaped inside link text
        result = channel_link("My_Channel", "mychannel")
        assert "My_Channel" in result
        assert "https://t.me/mychannel" in result


# ─── format_channel_stats_for_group ──────────────────────────────────────────

class TestFormatChannelStatsForGroup:
    def _make_channel(self, **kwargs):
        ch = MagicMock()
        ch.name = kwargs.get("name", "TestChannel")
        ch.username = kwargs.get("username", "testchannel")
        ch.subscribers = kwargs.get("subscribers", 10000)
        ch.avg_reach_24h = kwargs.get("avg_reach_24h", 1000)
        ch.avg_reach_48h = kwargs.get("avg_reach_48h", 1500)
        ch.avg_reach_72h = kwargs.get("avg_reach_72h", 1800)
        ch.err24_percent = kwargs.get("err24_percent", 5.5)
        ch.err_percent = kwargs.get("err_percent", 5.5)
        return ch

    def test_contains_channel_name(self):
        ch = self._make_channel(name="SuperChannel")
        result = format_channel_stats_for_group(ch)
        assert "SuperChannel" in result

    def test_contains_subscriber_count(self):
        ch = self._make_channel(subscribers=5000)
        result = format_channel_stats_for_group(ch)
        assert "5,000" in result or "5000" in result

    def test_contains_reach_values(self):
        ch = self._make_channel(avg_reach_24h=400, avg_reach_48h=600, avg_reach_72h=800)
        result = format_channel_stats_for_group(ch)
        assert "400" in result
        assert "600" in result
        assert "800" in result

    def test_contains_err_percent(self):
        ch = self._make_channel(err24_percent=7.25)
        result = format_channel_stats_for_group(ch)
        assert "7.25" in result

    def test_order_id_appended_when_provided(self):
        ch = self._make_channel()
        result = format_channel_stats_for_group(ch, order_id=42)
        assert "42" in result
        assert "Заказ" in result

    def test_no_order_id_line_when_not_provided(self):
        ch = self._make_channel()
        result = format_channel_stats_for_group(ch)
        assert "Заказ" not in result

    def test_channel_link_included(self):
        ch = self._make_channel(name="TestCh", username="testch")
        result = format_channel_stats_for_group(ch)
        assert "https://t.me/testch" in result

    def test_fallback_name_when_none(self):
        ch = self._make_channel()
        ch.name = None
        result = format_channel_stats_for_group(ch)
        assert "Канал" in result

    def test_zero_reach_handled(self):
        ch = self._make_channel(avg_reach_24h=0, avg_reach_48h=0, avg_reach_72h=0)
        result = format_channel_stats_for_group(ch)
        assert "0" in result

    def test_err_falls_back_to_err_percent(self):
        ch = self._make_channel()
        ch.err24_percent = 0
        ch.err_percent = 3.14
        result = format_channel_stats_for_group(ch)
        assert "3.14" in result

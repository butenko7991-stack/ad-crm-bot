"""
Unit tests for the crosspost signature formatting logic in services/crosspost.py

Covers the pure text-composition behaviour inside crosspost_post_to_max without
hitting the network or database:
  - Signature appended correctly for plain-text signature
  - Signature formatted as Markdown link when "text | url" pattern used
  - Early-return (False) when no text content and no signature
  - can_crosspost_today logic with mocked limit/count helpers
  - get_crosspost_daily_limit parsing: valid int, 0 (unlimited), invalid value fallback
  - is_crosspost_enabled: various truthy/falsy string values
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from services import crosspost as crosspost_mod
from services.crosspost import (
    DEFAULT_DAILY_LIMIT,
    can_crosspost_today,
    get_crosspost_daily_limit,
    is_crosspost_enabled,
    crosspost_post_to_max,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_post(content="Hello World", signature=None, post_id=1):
    post = MagicMock()
    post.id = post_id
    post.content = content
    post.signature = signature
    post.max_post_id = None
    post.max_posted_at = None
    return post


# ─── is_crosspost_enabled ─────────────────────────────────────────────────────

class TestIsCrosspostEnabled:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("val,expected", [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("Yes", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
        (None, False),
    ])
    async def test_enabled_values(self, val, expected):
        with patch("services.crosspost.get_setting", new=AsyncMock(return_value=val)):
            result = await is_crosspost_enabled()
        assert result is expected


# ─── get_crosspost_daily_limit ────────────────────────────────────────────────

class TestGetCrosspostDailyLimit:
    @pytest.mark.asyncio
    async def test_returns_integer_from_setting(self):
        with patch("services.crosspost.get_setting", new=AsyncMock(return_value="15")):
            assert await get_crosspost_daily_limit() == 15

    @pytest.mark.asyncio
    async def test_zero_means_no_limit(self):
        with patch("services.crosspost.get_setting", new=AsyncMock(return_value="0")):
            assert await get_crosspost_daily_limit() == 0

    @pytest.mark.asyncio
    async def test_invalid_value_falls_back_to_default(self):
        with patch("services.crosspost.get_setting", new=AsyncMock(return_value="not_a_number")):
            assert await get_crosspost_daily_limit() == DEFAULT_DAILY_LIMIT

    @pytest.mark.asyncio
    async def test_none_value_falls_back_to_default(self):
        with patch("services.crosspost.get_setting", new=AsyncMock(return_value=None)):
            assert await get_crosspost_daily_limit() == DEFAULT_DAILY_LIMIT

    @pytest.mark.asyncio
    async def test_negative_value_clamped_to_zero(self):
        with patch("services.crosspost.get_setting", new=AsyncMock(return_value="-5")):
            assert await get_crosspost_daily_limit() == 0

    @pytest.mark.asyncio
    async def test_large_value_accepted(self):
        with patch("services.crosspost.get_setting", new=AsyncMock(return_value="1000")):
            assert await get_crosspost_daily_limit() == 1000


# ─── can_crosspost_today ──────────────────────────────────────────────────────

class TestCanCrosspostToday:
    @pytest.mark.asyncio
    async def test_returns_true_when_limit_is_zero(self):
        with patch("services.crosspost.get_crosspost_daily_limit", new=AsyncMock(return_value=0)):
            result = await can_crosspost_today()
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_when_count_below_limit(self):
        with patch("services.crosspost.get_crosspost_daily_limit", new=AsyncMock(return_value=10)), \
             patch("services.crosspost.get_daily_crosspost_count", new=AsyncMock(return_value=5)):
            result = await can_crosspost_today()
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_count_equals_limit(self):
        with patch("services.crosspost.get_crosspost_daily_limit", new=AsyncMock(return_value=10)), \
             patch("services.crosspost.get_daily_crosspost_count", new=AsyncMock(return_value=10)):
            result = await can_crosspost_today()
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_count_exceeds_limit(self):
        with patch("services.crosspost.get_crosspost_daily_limit", new=AsyncMock(return_value=10)), \
             patch("services.crosspost.get_daily_crosspost_count", new=AsyncMock(return_value=15)):
            result = await can_crosspost_today()
        assert result is False


# ─── crosspost_post_to_max: early-return cases ───────────────────────────────

class TestCrosspostPostToMaxEarlyReturn:
    @pytest.mark.asyncio
    async def test_returns_false_when_disabled(self):
        post = _make_post()
        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=False)):
            result = await crosspost_post_to_max(post, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_daily_limit_reached(self):
        post = _make_post()
        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=False)):
            result = await crosspost_post_to_max(post, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_chat_id(self):
        post = _make_post()
        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=None)):
            result = await crosspost_post_to_max(post, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_content_empty_and_no_signature(self):
        post = _make_post(content="", signature=None)
        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=12345)):
            result = await crosspost_post_to_max(post, MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_content_none_and_no_signature(self):
        post = _make_post(content=None, signature=None)
        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=12345)):
            result = await crosspost_post_to_max(post, MagicMock())
        assert result is False


# ─── crosspost_post_to_max: signature formatting ─────────────────────────────

class TestCrosspostSignatureFormatting:
    """
    These tests intercept max_bot.send_message to capture the actual text
    that would be sent, verifying signature composition logic.
    """

    def _setup_patching(self):
        return {
            "is_crosspost_enabled": AsyncMock(return_value=True),
            "can_crosspost_today": AsyncMock(return_value=True),
            "get_max_crosspost_chat_id": AsyncMock(return_value=99999),
        }

    def _make_max_bot(self, message_id=42):
        bot = MagicMock()
        sent_msg = MagicMock()
        sent_msg.message_id = message_id
        bot.send_message = AsyncMock(return_value=sent_msg)
        return bot

    @pytest.mark.asyncio
    async def test_plain_signature_appended(self):
        post = _make_post(content="Ad content", signature="My Signature")
        max_bot = self._make_max_bot()

        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=99999)), \
             patch("services.crosspost.utc_now", return_value=MagicMock()):
            await crosspost_post_to_max(post, max_bot)

        sent_text = max_bot.send_message.call_args[1]["text"]
        assert "Ad content" in sent_text
        assert "My Signature" in sent_text

    @pytest.mark.asyncio
    async def test_pipe_signature_formatted_as_markdown_link(self):
        post = _make_post(content="Ad content", signature="My Channel | https://t.me/mychannel")
        max_bot = self._make_max_bot()

        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=99999)), \
             patch("services.crosspost.utc_now", return_value=MagicMock()):
            await crosspost_post_to_max(post, max_bot)

        sent_text = max_bot.send_message.call_args[1]["text"]
        assert "[My Channel](https://t.me/mychannel)" in sent_text

    @pytest.mark.asyncio
    async def test_pipe_with_non_http_url_falls_back_to_plain(self):
        post = _make_post(content="Content", signature="Label | not_a_url")
        max_bot = self._make_max_bot()

        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=99999)), \
             patch("services.crosspost.utc_now", return_value=MagicMock()):
            await crosspost_post_to_max(post, max_bot)

        sent_text = max_bot.send_message.call_args[1]["text"]
        # Should include the full signature as plain text, not as a link
        assert "Label | not_a_url" in sent_text
        assert "[Label]" not in sent_text

    @pytest.mark.asyncio
    async def test_no_signature_sends_content_only(self):
        post = _make_post(content="Just content", signature=None)
        max_bot = self._make_max_bot()

        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=99999)), \
             patch("services.crosspost.utc_now", return_value=MagicMock()):
            await crosspost_post_to_max(post, max_bot)

        sent_text = max_bot.send_message.call_args[1]["text"]
        assert sent_text == "Just content"

    @pytest.mark.asyncio
    async def test_only_signature_sent_when_no_content(self):
        post = _make_post(content="", signature="Channel | https://t.me/ch")
        max_bot = self._make_max_bot()

        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=99999)), \
             patch("services.crosspost.utc_now", return_value=MagicMock()):
            await crosspost_post_to_max(post, max_bot)

        sent_text = max_bot.send_message.call_args[1]["text"]
        assert "[Channel](https://t.me/ch)" == sent_text

    @pytest.mark.asyncio
    async def test_max_post_id_set_on_success(self):
        post = _make_post(content="Content")
        max_bot = self._make_max_bot(message_id=777)

        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=99999)), \
             patch("services.crosspost.utc_now", return_value=MagicMock()):
            result = await crosspost_post_to_max(post, max_bot)

        assert result is True
        assert post.max_post_id == "777"

    @pytest.mark.asyncio
    async def test_returns_false_on_send_exception(self):
        post = _make_post(content="Content")
        max_bot = MagicMock()
        max_bot.send_message = AsyncMock(side_effect=Exception("Network error"))

        with patch("services.crosspost.is_crosspost_enabled", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.can_crosspost_today", new=AsyncMock(return_value=True)), \
             patch("services.crosspost.get_max_crosspost_chat_id", new=AsyncMock(return_value=99999)):
            result = await crosspost_post_to_max(post, max_bot)

        assert result is False

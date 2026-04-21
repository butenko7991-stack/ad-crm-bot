import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock, patch

from services.settings import is_daily_schedule_empty_reminder_enabled


class TestIsDailyScheduleEmptyReminderEnabled:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("val,expected", [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
        (None, False),
    ])
    async def test_parses_setting_values(self, val, expected):
        with patch("services.settings.get_setting", new=AsyncMock(return_value=val)):
            result = await is_daily_schedule_empty_reminder_enabled()
        assert result is expected

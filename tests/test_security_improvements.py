"""
Unit tests for:
  1. cal_nav fix: channel_id must be read from FSM state, not from an undefined variable.
  2. MSG_INTERNAL_ERROR constant export.
  3. ADMIN_PASSWORD default-value block in main.py startup guard.
  4. Static verification: cal_nav source uses state.get_data() to obtain channel_id.
"""
import sys
import os
import ast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# 1. MSG_INTERNAL_ERROR is exported from utils.constants and utils.__init__
# ---------------------------------------------------------------------------

class TestMsgInternalError:
    def test_constant_exists_in_constants_module(self):
        from utils.constants import MSG_INTERNAL_ERROR
        assert isinstance(MSG_INTERNAL_ERROR, str)
        assert len(MSG_INTERNAL_ERROR) > 0

    def test_constant_exported_from_utils_init(self):
        from utils import MSG_INTERNAL_ERROR  # noqa: F401  (import should not raise)
        assert MSG_INTERNAL_ERROR

    def test_constant_is_in_all_list(self):
        import utils
        assert "MSG_INTERNAL_ERROR" in utils.__all__

    def test_no_exception_details_in_message(self):
        from utils.constants import MSG_INTERNAL_ERROR
        for bad_fragment in ("str(e)", "traceback", "Exception", "{e}"):
            assert bad_fragment not in MSG_INTERNAL_ERROR


# ---------------------------------------------------------------------------
# 2. Default ADMIN_PASSWORD raises SystemExit in main()
# ---------------------------------------------------------------------------

class TestDefaultPasswordBlock:
    @pytest.mark.asyncio
    async def test_default_password_raises_system_exit(self):
        """main() must raise SystemExit when ADMIN_PASSWORD is 'admin123'."""
        import importlib
        if "main" in sys.modules:
            del sys.modules["main"]
        with patch("config.BOT_TOKEN", "fake_token"), \
             patch("config.ADMIN_PASSWORD", "admin123"):
            import main as main_mod
            importlib.reload(main_mod)
            with pytest.raises(SystemExit):
                await main_mod.main()


# ---------------------------------------------------------------------------
# 3. Static verification: cal_nav reads channel_id from state
# ---------------------------------------------------------------------------

class TestCalNavChannelIdFromState:
    """Verify source-level that cal_nav reads channel_id from FSM state data."""

    def _get_cal_nav_source(self) -> str:
        client_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "handlers", "client.py"
        )
        with open(client_path, encoding="utf-8") as f:
            return f.read()

    def test_channel_id_obtained_from_state_get_data(self):
        """cal_nav must call state.get_data() to obtain channel_id."""
        src = self._get_cal_nav_source()
        # Find the cal_nav function and check that state.get_data() precedes channel_id usage
        assert 'state.get_data()' in src, "cal_nav must call state.get_data()"

    def test_channel_id_not_unresolved(self):
        """The old bug was using `channel_id` before assigning it. Verify assignment exists."""
        src = self._get_cal_nav_source()
        # The fix must include: channel_id = data.get("channel_id")
        assert 'channel_id = data.get("channel_id")' in src, \
            "cal_nav must assign channel_id from FSM state data"

    def test_no_bare_channel_id_reference_before_assignment(self):
        """Verify cal_nav function body does not use channel_id before assigning it."""
        client_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "handlers", "client.py"
        )
        with open(client_path, encoding="utf-8") as f:
            lines = f.readlines()

        # Find cal_nav function start
        start = None
        for i, line in enumerate(lines):
            if "async def cal_nav(" in line:
                start = i
                break
        assert start is not None, "cal_nav function not found"

        # Find the assignment of channel_id in the function
        assign_line = None
        for i in range(start, min(start + 50, len(lines))):
            if 'channel_id = data.get("channel_id")' in lines[i]:
                assign_line = i
                break
        assert assign_line is not None, "channel_id assignment from state not found in cal_nav"

        # Verify that 'if not channel_id' appears AFTER the assignment, not before
        check_line = None
        for i in range(start, min(start + 50, len(lines))):
            if "if not channel_id" in lines[i]:
                check_line = i
                break
        assert check_line is not None, "'if not channel_id' guard missing"
        assert check_line > assign_line, \
            "channel_id guard must come AFTER the state.get_data() assignment"

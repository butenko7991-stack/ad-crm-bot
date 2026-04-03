"""
Unit tests for the pure (non-DB) logic in services/gamification.py

Covers:
  - GamificationService._calculate_level: XP → level mapping
  - XP calculation formula used in process_sale
  - GamificationService.process_sale result structure (with DB mocked)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from services.gamification import GamificationService
from config import MANAGER_LEVELS


class TestCalculateLevel:
    """Tests for the pure _calculate_level helper."""

    def setup_method(self):
        self.svc = GamificationService()

    @pytest.mark.parametrize("xp,expected_level", [
        (0, 1),
        (1, 1),
        (199, 1),
        (200, 2),
        (500, 2),
        (799, 2),
        (800, 3),
        (1000, 3),
        (1999, 3),
        (2000, 4),
        (3000, 4),
        (4999, 4),
        (5000, 5),
        (9999, 5),
        (100000, 5),
    ])
    def test_level_boundaries(self, xp, expected_level):
        assert self.svc._calculate_level(xp) == expected_level

    def test_level_1_at_zero_xp(self):
        assert self.svc._calculate_level(0) == 1

    def test_level_5_is_maximum(self):
        assert self.svc._calculate_level(10_000_000) == 5

    def test_level_increases_monotonically(self):
        xp_values = [0, 100, 200, 500, 800, 1500, 2000, 3000, 5000, 8000]
        levels = [self.svc._calculate_level(xp) for xp in xp_values]
        for i in range(1, len(levels)):
            assert levels[i] >= levels[i - 1], f"Level decreased at xp={xp_values[i]}"


class TestXpCalculationFormula:
    """Tests for the XP calculation used in process_sale."""

    def _calculate_xp(self, order_amount: float) -> int:
        """Mirror the formula used in GamificationService.process_sale."""
        return 10 + int(order_amount / 100)

    def test_minimum_xp_for_zero_amount(self):
        assert self._calculate_xp(0) == 10

    def test_xp_for_100_ruble_order(self):
        assert self._calculate_xp(100) == 11

    def test_xp_for_1000_ruble_order(self):
        assert self._calculate_xp(1000) == 20

    def test_xp_for_10000_ruble_order(self):
        assert self._calculate_xp(10000) == 110

    def test_xp_for_fractional_amount_truncated(self):
        # int(99 / 100) = 0, so xp = 10
        assert self._calculate_xp(99) == 10
        # int(199 / 100) = 1, so xp = 11
        assert self._calculate_xp(199) == 11

    def test_xp_grows_with_order_amount(self):
        amounts = [0, 500, 1000, 5000, 10000]
        xp_values = [self._calculate_xp(a) for a in amounts]
        for i in range(1, len(xp_values)):
            assert xp_values[i] > xp_values[i - 1]


class TestProcessSaleWithMockedDB:
    """Tests for GamificationService.process_sale with DB mocked."""

    def setup_method(self):
        self.svc = GamificationService()

    async def _mock_add_experience(self, manager_id, xp, reason=""):
        return {
            "new_xp": 210,
            "level_up": True,
            "new_level": 2,
            "level_name": MANAGER_LEVELS[2]["name"],
        }

    @pytest.mark.asyncio
    async def test_process_sale_returns_expected_keys(self):
        with patch.object(self.svc, "add_experience", new=AsyncMock(return_value={
            "new_xp": 110,
            "level_up": False,
            "new_level": 1,
        })), patch.object(self.svc, "check_achievements", new=AsyncMock(return_value=[])):
            result = await self.svc.process_sale(manager_id=1, order_amount=1000.0)

        assert "xp_gained" in result
        assert "level_up" in result
        assert "new_level" in result
        assert "achievements" in result

    @pytest.mark.asyncio
    async def test_process_sale_xp_gained_matches_formula(self):
        captured_xp = {}

        async def mock_add_exp(manager_id, xp, reason=""):
            captured_xp["xp"] = xp
            return {"new_xp": xp, "level_up": False, "new_level": 1}

        with patch.object(self.svc, "add_experience", new=mock_add_exp), \
             patch.object(self.svc, "check_achievements", new=AsyncMock(return_value=[])):
            await self.svc.process_sale(manager_id=1, order_amount=5000.0)

        # 10 + int(5000 / 100) = 10 + 50 = 60
        assert captured_xp["xp"] == 60

    @pytest.mark.asyncio
    async def test_process_sale_level_up_propagated(self):
        with patch.object(self.svc, "add_experience", new=AsyncMock(return_value={
            "new_xp": 210,
            "level_up": True,
            "new_level": 2,
            "level_name": "Джуниор",
        })), patch.object(self.svc, "check_achievements", new=AsyncMock(return_value=[])):
            result = await self.svc.process_sale(manager_id=1, order_amount=100.0)

        assert result["level_up"] is True
        assert result["new_level"] == 2
        assert result["level_name"] == "Джуниор"

    @pytest.mark.asyncio
    async def test_process_sale_achievements_propagated(self):
        achievements = ["🎯 Первая продажа!", "🏆 5 продаж!"]
        with patch.object(self.svc, "add_experience", new=AsyncMock(return_value={
            "new_xp": 50, "level_up": False, "new_level": 1
        })), patch.object(self.svc, "check_achievements", new=AsyncMock(return_value=achievements)):
            result = await self.svc.process_sale(manager_id=1, order_amount=0)

        assert result["achievements"] == achievements

    @pytest.mark.asyncio
    async def test_process_sale_no_level_up_returns_none_level_name(self):
        with patch.object(self.svc, "add_experience", new=AsyncMock(return_value={
            "new_xp": 50, "level_up": False, "new_level": 1
        })), patch.object(self.svc, "check_achievements", new=AsyncMock(return_value=[])):
            result = await self.svc.process_sale(manager_id=1, order_amount=0)

        assert result.get("level_name") is None


class TestAddExperienceWithMockedDB:
    """Tests for GamificationService.add_experience with DB session mocked."""

    def setup_method(self):
        self.svc = GamificationService()

    def _make_manager(self, xp=100, level=1):
        m = MagicMock()
        m.experience_points = xp
        m.level = level
        m.commission_rate = Decimal("10")
        return m

    @pytest.mark.asyncio
    async def test_manager_not_found_returns_error(self):
        mock_session = AsyncMock()
        mock_session.get.return_value = None
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gamification.async_session_maker", return_value=mock_session):
            result = await self.svc.add_experience(manager_id=999, xp=50)

        assert "error" in result

    @pytest.mark.asyncio
    async def test_xp_added_to_manager(self):
        manager = self._make_manager(xp=100, level=1)
        mock_session = AsyncMock()
        mock_session.get.return_value = manager
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gamification.async_session_maker", return_value=mock_session):
            result = await self.svc.add_experience(manager_id=1, xp=50)

        assert manager.experience_points == 150
        assert result["new_xp"] == 150

    @pytest.mark.asyncio
    async def test_level_up_detected(self):
        manager = self._make_manager(xp=190, level=1)
        mock_session = AsyncMock()
        mock_session.get.return_value = manager
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gamification.async_session_maker", return_value=mock_session):
            result = await self.svc.add_experience(manager_id=1, xp=20)

        assert result["level_up"] is True
        assert result["new_level"] == 2
        assert "level_name" in result

    @pytest.mark.asyncio
    async def test_no_level_up_when_xp_stays_within_level(self):
        manager = self._make_manager(xp=50, level=1)
        mock_session = AsyncMock()
        mock_session.get.return_value = manager
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gamification.async_session_maker", return_value=mock_session):
            result = await self.svc.add_experience(manager_id=1, xp=50)

        assert result["level_up"] is False

    @pytest.mark.asyncio
    async def test_commission_rate_updated_on_level_up(self):
        manager = self._make_manager(xp=195, level=1)
        mock_session = AsyncMock()
        mock_session.get.return_value = manager
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gamification.async_session_maker", return_value=mock_session):
            await self.svc.add_experience(manager_id=1, xp=10)

        expected_commission = Decimal(str(MANAGER_LEVELS[2]["commission"]))
        assert manager.commission_rate == expected_commission


class TestManagerLevelsConfig:
    """Validate the MANAGER_LEVELS config used by gamification."""

    def test_all_5_levels_defined(self):
        assert set(MANAGER_LEVELS.keys()) == {1, 2, 3, 4, 5}

    def test_level_1_starts_at_zero_xp(self):
        assert MANAGER_LEVELS[1]["min_sales"] == 0

    def test_commission_increases_with_level(self):
        commissions = [MANAGER_LEVELS[lvl]["commission"] for lvl in sorted(MANAGER_LEVELS)]
        for i in range(1, len(commissions)):
            assert commissions[i] > commissions[i - 1], (
                f"Commission did not increase from level {i} to {i+1}"
            )

    def test_all_levels_have_required_keys(self):
        for lvl, info in MANAGER_LEVELS.items():
            for key in ("name", "emoji", "commission", "min_sales"):
                assert key in info, f"Level {lvl} missing key '{key}'"

    def test_level_names_are_non_empty_strings(self):
        for lvl, info in MANAGER_LEVELS.items():
            assert isinstance(info["name"], str) and info["name"]

    def test_level_5_has_highest_commission(self):
        max_commission = max(info["commission"] for info in MANAGER_LEVELS.values())
        assert MANAGER_LEVELS[5]["commission"] == max_commission

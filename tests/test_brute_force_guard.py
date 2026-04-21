"""
Unit tests for BruteForceGuard in utils/auth.py.

Tests cover:
  - is_locked() returns False before threshold is reached
  - is_locked() returns True after threshold is reached
  - record_failure() returns the correct attempt count
  - clear() resets the failure history → is_locked() returns False again
  - unlock_in() returns a positive timedelta when locked, timedelta(0) when not
  - Attempts outside the time window are pruned automatically
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import timedelta

import pytest

from utils.auth import BruteForceGuard, BRUTE_FORCE_MAX_ATTEMPTS


class TestBruteForceGuard:
    @pytest.fixture
    def guard(self):
        # Use small values for fast testing
        return BruteForceGuard(max_attempts=3, window=timedelta(minutes=5))

    def _now_utc(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(tzinfo=None)

    # ── is_locked ────────────────────────────────────────────────────────────

    def test_not_locked_initially(self, guard):
        assert guard.is_locked(user_id=42) is False

    def test_not_locked_below_threshold(self, guard):
        for _ in range(2):
            guard.record_failure(42)
        assert guard.is_locked(42) is False

    def test_locked_at_threshold(self, guard):
        for _ in range(3):
            guard.record_failure(42)
        assert guard.is_locked(42) is True

    def test_locked_above_threshold(self, guard):
        for _ in range(10):
            guard.record_failure(42)
        assert guard.is_locked(42) is True

    def test_different_users_isolated(self, guard):
        for _ in range(3):
            guard.record_failure(1)
        assert guard.is_locked(1) is True
        assert guard.is_locked(2) is False

    # ── record_failure ───────────────────────────────────────────────────────

    def test_record_failure_increments_count(self, guard):
        assert guard.record_failure(99) == 1
        assert guard.record_failure(99) == 2
        assert guard.record_failure(99) == 3

    # ── clear ────────────────────────────────────────────────────────────────

    def test_clear_removes_lock(self, guard):
        for _ in range(3):
            guard.record_failure(7)
        assert guard.is_locked(7) is True
        guard.clear(7)
        assert guard.is_locked(7) is False

    def test_clear_nonexistent_user_is_safe(self, guard):
        guard.clear(9999)  # should not raise

    # ── unlock_in ────────────────────────────────────────────────────────────

    def test_unlock_in_zero_when_not_locked(self, guard):
        assert guard.unlock_in(42) == timedelta(0)

    def test_unlock_in_positive_when_locked(self, guard):
        for _ in range(3):
            guard.record_failure(5)
        remaining = guard.unlock_in(5)
        assert remaining > timedelta(0)
        assert remaining <= timedelta(minutes=5)

    # ── time-window pruning ──────────────────────────────────────────────────

    def test_old_attempts_are_pruned(self):
        """Failures older than the window should not count."""
        g = BruteForceGuard(max_attempts=3, window=timedelta(minutes=5))

        # Inject two "old" failures directly into internal state
        past = self._now_utc() - timedelta(minutes=10)
        g._failures[55] = [past, past]  # these are outside the window

        # One fresh failure — total in-window count should be 1, not 3
        g.record_failure(55)
        assert g.is_locked(55) is False

    def test_exactly_max_fresh_failures_causes_lock(self, guard):
        """Exactly max_attempts in-window failures should lock."""
        for _ in range(3):
            guard.record_failure(77)
        assert guard.is_locked(77) is True

    # ── default constants ─────────────────────────────────────────────────────

    def test_default_max_attempts_is_5(self):
        assert BRUTE_FORCE_MAX_ATTEMPTS == 5

    def test_default_guard_uses_5_attempts(self):
        g = BruteForceGuard()
        for _ in range(4):
            g.record_failure(1)
        assert g.is_locked(1) is False
        g.record_failure(1)
        assert g.is_locked(1) is True

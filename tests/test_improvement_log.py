"""
Unit tests for services/improvement_log.py

Covers file-based I/O functions that require no DB or external services:
  - log_improvement
  - get_recent_improvements
  - format_improvement_entry
  - get_improvement_stats
"""
import json
import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.improvement_log as imp_log_mod
from services.improvement_log import (
    log_improvement,
    get_recent_improvements,
    format_improvement_entry,
    get_improvement_stats,
    CATEGORIES,
)


@pytest.fixture(autouse=True)
def tmp_log_path(tmp_path, monkeypatch):
    """Redirect the log file to a temp directory for every test."""
    log_file = tmp_path / "improvement_log.json"
    monkeypatch.setattr(imp_log_mod, "IMPROVEMENT_LOG_PATH", log_file)
    yield log_file


# ─── log_improvement ──────────────────────────────────────────────────────────

class TestLogImprovement:
    def test_creates_file_when_missing(self, tmp_log_path):
        assert not tmp_log_path.exists()
        log_improvement("Title", "Description")
        assert tmp_log_path.exists()

    def test_entry_has_required_keys(self, tmp_log_path):
        log_improvement("My title", "My desc", category="bugfix", author="admin:1")
        entries = json.loads(tmp_log_path.read_text())
        assert len(entries) == 1
        entry = entries[0]
        assert entry["title"] == "My title"
        assert entry["description"] == "My desc"
        assert entry["category"] == "bugfix"
        assert entry["author"] == "admin:1"
        assert "ts" in entry

    def test_timestamp_format(self, tmp_log_path):
        log_improvement("T", "D")
        entry = json.loads(tmp_log_path.read_text())[0]
        # Should be ISO-like: 2024-01-01T12:00:00Z
        ts = entry["ts"]
        assert "T" in ts and ts.endswith("Z")

    def test_appends_multiple_entries(self, tmp_log_path):
        log_improvement("First", "Desc1")
        log_improvement("Second", "Desc2")
        entries = json.loads(tmp_log_path.read_text())
        assert len(entries) == 2
        assert entries[0]["title"] == "First"
        assert entries[1]["title"] == "Second"

    def test_title_truncated_at_200_chars(self, tmp_log_path):
        long_title = "x" * 300
        log_improvement(long_title, "Desc")
        entry = json.loads(tmp_log_path.read_text())[0]
        assert len(entry["title"]) <= 200

    def test_description_truncated_at_2000_chars(self, tmp_log_path):
        long_desc = "y" * 3000
        log_improvement("Title", long_desc)
        entry = json.loads(tmp_log_path.read_text())[0]
        assert len(entry["description"]) <= 2000

    def test_max_entries_limit_enforced(self, tmp_log_path, monkeypatch):
        monkeypatch.setattr(imp_log_mod, "IMPROVEMENT_LOG_MAX_ENTRIES", 5)
        for i in range(7):
            log_improvement(f"Title {i}", "Desc")
        entries = json.loads(tmp_log_path.read_text())
        assert len(entries) == 5
        # Latest entries should be kept
        assert entries[-1]["title"] == "Title 6"

    def test_overwrites_non_list_file(self, tmp_log_path):
        tmp_log_path.write_text('{"not": "a list"}', encoding="utf-8")
        log_improvement("Title", "Desc")
        entries = json.loads(tmp_log_path.read_text())
        assert isinstance(entries, list)
        assert len(entries) == 1

    def test_overwrites_invalid_json_file(self, tmp_log_path):
        tmp_log_path.write_text("not json at all", encoding="utf-8")
        log_improvement("Title", "Desc")
        entries = json.loads(tmp_log_path.read_text())
        assert len(entries) == 1

    def test_default_category_is_admin_note(self, tmp_log_path):
        log_improvement("Title", "Desc")
        entry = json.loads(tmp_log_path.read_text())[0]
        assert entry["category"] == "admin_note"

    def test_default_author_is_system(self, tmp_log_path):
        log_improvement("Title", "Desc")
        entry = json.loads(tmp_log_path.read_text())[0]
        assert entry["author"] == "system"


# ─── get_recent_improvements ──────────────────────────────────────────────────

class TestGetRecentImprovements:
    def test_returns_empty_when_no_file(self, tmp_log_path):
        assert get_recent_improvements() == []

    def test_returns_entries_in_reverse_order(self, tmp_log_path):
        for i in range(3):
            log_improvement(f"Title {i}", "Desc")
        results = get_recent_improvements(limit=3)
        assert results[0]["title"] == "Title 2"
        assert results[2]["title"] == "Title 0"

    def test_limit_respected(self, tmp_log_path):
        for i in range(10):
            log_improvement(f"T{i}", "D")
        results = get_recent_improvements(limit=3)
        assert len(results) == 3

    def test_category_filter(self, tmp_log_path):
        log_improvement("Bug", "D", category="bugfix")
        log_improvement("Feature", "D", category="feature")
        log_improvement("Another bug", "D", category="bugfix")
        results = get_recent_improvements(category="bugfix")
        assert all(e["category"] == "bugfix" for e in results)
        assert len(results) == 2

    def test_category_filter_no_match_returns_empty(self, tmp_log_path):
        log_improvement("T", "D", category="bugfix")
        results = get_recent_improvements(category="security")
        assert results == []

    def test_invalid_json_returns_empty(self, tmp_log_path):
        tmp_log_path.write_text("invalid json", encoding="utf-8")
        results = get_recent_improvements()
        assert results == []

    def test_non_list_json_returns_empty(self, tmp_log_path):
        tmp_log_path.write_text('{"key": "value"}', encoding="utf-8")
        results = get_recent_improvements()
        assert results == []

    def test_default_limit_is_10(self, tmp_log_path):
        for i in range(15):
            log_improvement(f"T{i}", "D")
        results = get_recent_improvements()
        assert len(results) == 10


# ─── format_improvement_entry ─────────────────────────────────────────────────

class TestFormatImprovementEntry:
    def test_contains_title(self):
        entry = {"title": "My Improvement", "category": "bugfix", "ts": "2024-01-01T12:00:00Z", "description": "Fixed it", "author": "admin:1"}
        result = format_improvement_entry(entry, index=0)
        assert "My Improvement" in result

    def test_contains_category_label(self):
        entry = {"title": "T", "category": "bugfix", "ts": "2024-01-01T12:00:00Z", "description": "D", "author": "system"}
        result = format_improvement_entry(entry, index=0)
        assert "Исправление ошибки" in result or "bugfix" in result

    def test_contains_author(self):
        entry = {"title": "T", "category": "feature", "ts": "2024-01-01T12:00:00Z", "description": "D", "author": "admin:42"}
        result = format_improvement_entry(entry, index=0)
        assert "admin:42" in result

    def test_description_truncated_in_output(self):
        long_desc = "x" * 500
        entry = {"title": "T", "category": "feature", "ts": "2024-01-01T12:00:00Z", "description": long_desc, "author": "system"}
        result = format_improvement_entry(entry, index=0)
        assert "…" in result

    def test_short_description_not_truncated(self):
        entry = {"title": "T", "category": "feature", "ts": "2024-01-01T12:00:00Z", "description": "Short desc", "author": "system"}
        result = format_improvement_entry(entry, index=0)
        assert "Short desc" in result
        assert "…" not in result

    def test_index_shown_as_1_based(self):
        entry = {"title": "T", "category": "admin_note", "ts": "2024-01-01T12:00:00Z", "description": "D", "author": "system"}
        result = format_improvement_entry(entry, index=2)
        assert "3." in result

    def test_unknown_category_uses_key_as_fallback(self):
        entry = {"title": "T", "category": "unknown_cat", "ts": "2024-01-01T12:00:00Z", "description": "D", "author": "system"}
        result = format_improvement_entry(entry, index=0)
        assert "unknown_cat" in result

    def test_timestamp_formatted(self):
        entry = {"title": "T", "category": "admin_note", "ts": "2024-06-15T14:30:00Z", "description": "D", "author": "system"}
        result = format_improvement_entry(entry, index=0)
        assert "2024-06-15 14:30" in result


# ─── get_improvement_stats ────────────────────────────────────────────────────

class TestGetImprovementStats:
    def test_empty_when_no_file(self, tmp_log_path):
        stats = get_improvement_stats()
        assert stats == {"total": 0, "by_category": {}}

    def test_counts_total_entries(self, tmp_log_path):
        log_improvement("T1", "D", category="bugfix")
        log_improvement("T2", "D", category="feature")
        log_improvement("T3", "D", category="bugfix")
        stats = get_improvement_stats()
        assert stats["total"] == 3

    def test_counts_by_category(self, tmp_log_path):
        log_improvement("T1", "D", category="bugfix")
        log_improvement("T2", "D", category="feature")
        log_improvement("T3", "D", category="bugfix")
        stats = get_improvement_stats()
        assert stats["by_category"]["bugfix"] == 2
        assert stats["by_category"]["feature"] == 1

    def test_invalid_json_returns_empty(self, tmp_log_path):
        tmp_log_path.write_text("bad json", encoding="utf-8")
        stats = get_improvement_stats()
        assert stats == {"total": 0, "by_category": {}}

    def test_non_list_json_returns_empty(self, tmp_log_path):
        tmp_log_path.write_text('{"not": "list"}', encoding="utf-8")
        stats = get_improvement_stats()
        assert stats == {"total": 0, "by_category": {}}

    def test_missing_category_key_counted_as_unknown(self, tmp_log_path):
        tmp_log_path.write_text('[{"title": "T", "description": "D"}]', encoding="utf-8")
        stats = get_improvement_stats()
        assert stats["by_category"].get("unknown", 0) == 1


# ─── CATEGORIES dict ─────────────────────────────────────────────────────────

class TestCategoriesConstant:
    def test_all_expected_categories_present(self):
        expected = {"ai_suggestion", "bugfix", "feature", "refactor", "security", "performance", "admin_note"}
        assert expected.issubset(set(CATEGORIES.keys()))

    def test_all_labels_are_non_empty_strings(self):
        for key, label in CATEGORIES.items():
            assert isinstance(label, str) and len(label) > 0, f"Empty label for category '{key}'"

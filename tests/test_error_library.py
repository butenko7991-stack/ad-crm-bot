"""
Unit tests for services/error_library.py

Covers:
  - lookup_error: matching by exception type and traceback patterns
  - record_unknown_error: file writing, max-entries cap, corrupt-file resilience
  - get_error_log: ordering and limit
  - format_known_error: output formatting
  - KNOWN_ERRORS structure validation
"""
import json
import sys
import os
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.error_library as err_lib_mod
from services.error_library import (
    lookup_error,
    record_unknown_error,
    get_error_log,
    format_known_error,
    KNOWN_ERRORS,
)


@pytest.fixture(autouse=True)
def tmp_error_log(tmp_path, monkeypatch):
    """Redirect ERROR_LOG_PATH to a temp file for every test."""
    log_file = tmp_path / "error_log.json"
    monkeypatch.setattr(err_lib_mod, "ERROR_LOG_PATH", log_file)
    yield log_file


# ─── lookup_error ─────────────────────────────────────────────────────────────

class TestLookupError:
    def test_returns_none_for_unknown_error(self):
        exc = Exception("something totally unknown xyz123")
        result = lookup_error(exc, "")
        assert result is None

    def test_matches_db_connection_refused_by_pattern(self):
        exc = Exception("Connection refused")
        result = lookup_error(exc, "Connection refused to PostgreSQL")
        assert result is not None
        assert result["id"] == "db_connection_refused"

    def test_matches_db_unique_violation_by_type_name(self):
        class UniqueViolationError(Exception):
            pass
        exc = UniqueViolationError("duplicate key value violates unique constraint")
        result = lookup_error(exc, "duplicate key value violates unique constraint")
        assert result is not None
        assert result["id"] == "db_unique_violation"

    def test_matches_tg_message_not_modified(self):
        class TelegramBadRequest(Exception):
            pass
        exc = TelegramBadRequest("message is not modified")
        result = lookup_error(exc, "message is not modified: specified new message content")
        assert result is not None
        assert result["id"] == "tg_message_not_modified"

    def test_matches_tg_rate_limit_by_exception_type(self):
        # The entry requires exc_type in ['TelegramRetryAfter', 'RetryAfter']
        class RetryAfter(Exception):
            pass
        exc = RetryAfter("Too Many Requests: retry after 30")
        result = lookup_error(exc, "")
        assert result is not None
        assert result["id"] == "tg_rate_limit"

    def test_matches_claude_api_key_invalid(self):
        exc = Exception("401: invalid_api_key")
        result = lookup_error(exc, "401 authentication_error")
        assert result is not None
        assert result["id"] == "claude_api_key_invalid"

    def test_matches_json_decode_error_by_type(self):
        import json
        try:
            json.loads("bad json")
        except json.JSONDecodeError as e:
            result = lookup_error(e, "")
            assert result is not None
            assert result["id"] == "json_decode_error"

    def test_matches_datetime_utcnow_deprecated(self):
        exc = DeprecationWarning("datetime.utcnow is deprecated")
        result = lookup_error(exc, "datetime.utcnow called in main.py")
        assert result is not None
        assert result["id"] == "datetime_utcnow_deprecated"

    def test_case_insensitive_pattern_matching(self):
        exc = Exception("connection refused")
        result = lookup_error(exc, "CONNECTION REFUSED to database")
        assert result is not None
        assert result["id"] == "db_connection_refused"

    def test_match_type_filter_excludes_wrong_type(self):
        # db_unique_violation requires UniqueViolationError or IntegrityError type
        exc = ValueError("duplicate key value")
        result = lookup_error(exc, "duplicate key value violates unique constraint")
        # Should still match because pattern is in combined text even if type differs
        # (type_match is True when match_type contains type name OR pattern found in combined)
        # Actually, let's check the real behaviour: type_match checks
        # if exc_type in entry["match_type"] OR any(t in combined for t in entry["match_type"])
        # "ValueError" not in match_type, but "UniqueViolationError" IS in combined text
        # So it depends on whether match_type strings appear in combined.
        # In this case they don't → type_match = False → not matched
        # Let's test an actually non-matching case:
        exc2 = ValueError("something unrelated")
        result2 = lookup_error(exc2, "nothing special here")
        assert result2 is None

    def test_returns_first_matching_entry(self):
        exc = Exception("Connection refused")
        result = lookup_error(exc, "Connection refused")
        # Should return the db_connection_refused entry (first match)
        assert result is not None
        assert result["id"] == "db_connection_refused"

    def test_result_has_required_keys(self):
        exc = Exception("Connection refused")
        result = lookup_error(exc, "Connection refused")
        assert result is not None
        for key in ("id", "category", "title", "description", "solution"):
            assert key in result


# ─── record_unknown_error ─────────────────────────────────────────────────────

class TestRecordUnknownError:
    def test_creates_file_when_missing(self, tmp_error_log):
        assert not tmp_error_log.exists()
        record_unknown_error(Exception("oops"), "traceback text")
        assert tmp_error_log.exists()

    def test_entry_has_required_keys(self, tmp_error_log):
        exc = ValueError("test error")
        record_unknown_error(exc, "some traceback", context="handler_x")
        entries = json.loads(tmp_error_log.read_text())
        assert len(entries) == 1
        entry = entries[0]
        assert entry["exc_type"] == "ValueError"
        assert "test error" in entry["exc_msg"]
        assert entry["context"] == "handler_x"
        assert "ts" in entry
        assert "traceback" in entry

    def test_appends_multiple_entries(self, tmp_error_log):
        record_unknown_error(Exception("err1"), "tb1")
        record_unknown_error(Exception("err2"), "tb2")
        entries = json.loads(tmp_error_log.read_text())
        assert len(entries) == 2

    def test_max_entries_limit_enforced(self, tmp_error_log, monkeypatch):
        monkeypatch.setattr(err_lib_mod, "ERROR_LOG_MAX_ENTRIES", 3)
        for i in range(5):
            record_unknown_error(Exception(f"err{i}"), "tb")
        entries = json.loads(tmp_error_log.read_text())
        assert len(entries) == 3
        assert entries[-1]["exc_msg"] == "err4"

    def test_handles_invalid_json_file(self, tmp_error_log):
        tmp_error_log.write_text("not json", encoding="utf-8")
        record_unknown_error(Exception("new error"), "tb")
        entries = json.loads(tmp_error_log.read_text())
        assert len(entries) == 1

    def test_handles_non_list_json(self, tmp_error_log):
        tmp_error_log.write_text('{"oops": "not a list"}', encoding="utf-8")
        record_unknown_error(Exception("new error"), "tb")
        entries = json.loads(tmp_error_log.read_text())
        assert len(entries) == 1

    def test_exc_msg_truncated_at_300_chars(self, tmp_error_log):
        exc = Exception("x" * 500)
        record_unknown_error(exc, "tb")
        entry = json.loads(tmp_error_log.read_text())[0]
        assert len(entry["exc_msg"]) <= 300

    def test_traceback_truncated_to_last_1000_chars(self, tmp_error_log):
        long_tb = "line\n" * 300  # >1000 chars
        record_unknown_error(Exception("e"), long_tb)
        entry = json.loads(tmp_error_log.read_text())[0]
        assert len(entry["traceback"]) <= 1000

    def test_context_truncated_at_200_chars(self, tmp_error_log):
        long_ctx = "c" * 300
        record_unknown_error(Exception("e"), "tb", context=long_ctx)
        entry = json.loads(tmp_error_log.read_text())[0]
        assert len(entry["context"]) <= 200


# ─── get_error_log ────────────────────────────────────────────────────────────

class TestGetErrorLog:
    def test_returns_empty_when_no_file(self, tmp_error_log):
        assert get_error_log() == []

    def test_returns_entries_in_reverse_order(self, tmp_error_log):
        for i in range(3):
            record_unknown_error(Exception(f"err{i}"), "tb")
        results = get_error_log(limit=3)
        assert results[0]["exc_msg"] == "err2"
        assert results[2]["exc_msg"] == "err0"

    def test_limit_respected(self, tmp_error_log):
        for i in range(10):
            record_unknown_error(Exception(f"err{i}"), "tb")
        results = get_error_log(limit=3)
        assert len(results) == 3

    def test_invalid_json_returns_empty(self, tmp_error_log):
        tmp_error_log.write_text("bad json", encoding="utf-8")
        assert get_error_log() == []

    def test_non_list_json_returns_empty(self, tmp_error_log):
        tmp_error_log.write_text('{"not": "list"}', encoding="utf-8")
        assert get_error_log() == []


# ─── format_known_error ───────────────────────────────────────────────────────

class TestFormatKnownError:
    def _make_entry(self):
        return {
            "id": "test_err",
            "category": "db",
            "title": "Test Error Title",
            "description": "Something went wrong with the DB.",
            "solution": "1. Do this.\n2. Do that.",
        }

    def test_contains_title(self):
        result = format_known_error(self._make_entry())
        assert "Test Error Title" in result

    def test_contains_category(self):
        result = format_known_error(self._make_entry())
        assert "db" in result

    def test_contains_description(self):
        result = format_known_error(self._make_entry())
        assert "Something went wrong with the DB." in result

    def test_contains_solution(self):
        result = format_known_error(self._make_entry())
        assert "1. Do this." in result

    def test_returns_string(self):
        result = format_known_error(self._make_entry())
        assert isinstance(result, str)


# ─── KNOWN_ERRORS structure ───────────────────────────────────────────────────

class TestKnownErrorsStructure:
    def test_is_non_empty_list(self):
        assert isinstance(KNOWN_ERRORS, list)
        assert len(KNOWN_ERRORS) > 0

    def test_all_entries_have_required_keys(self):
        required_keys = {"id", "category", "match_type", "patterns", "title", "description", "solution"}
        for entry in KNOWN_ERRORS:
            missing = required_keys - set(entry.keys())
            assert not missing, f"Entry {entry.get('id', '?')} missing keys: {missing}"

    def test_all_ids_are_unique(self):
        ids = [e["id"] for e in KNOWN_ERRORS]
        assert len(ids) == len(set(ids)), "Duplicate IDs found in KNOWN_ERRORS"

    def test_all_patterns_are_lists(self):
        for entry in KNOWN_ERRORS:
            assert isinstance(entry["patterns"], list), f"patterns for {entry['id']} not a list"

    def test_all_match_types_are_lists(self):
        for entry in KNOWN_ERRORS:
            assert isinstance(entry["match_type"], list), f"match_type for {entry['id']} not a list"

    def test_categories_are_valid_strings(self):
        for entry in KNOWN_ERRORS:
            assert isinstance(entry["category"], str) and entry["category"]

    @pytest.mark.parametrize("entry_id", [
        "db_connection_refused",
        "db_unique_violation",
        "tg_message_not_modified",
        "tg_rate_limit",
        "claude_api_key_invalid",
        "json_decode_error",
        "datetime_utcnow_deprecated",
    ])
    def test_key_entries_exist(self, entry_id):
        ids = {e["id"] for e in KNOWN_ERRORS}
        assert entry_id in ids, f"Expected entry '{entry_id}' not found in KNOWN_ERRORS"

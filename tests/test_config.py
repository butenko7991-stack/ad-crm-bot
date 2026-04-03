"""
Unit tests for config.py

Validates the integrity and consistency of the configuration data structures
that drive business logic (pricing, levels, formats, loyalty discounts, etc.).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import timedelta

from config import (
    CHANNEL_CATEGORIES,
    PLACEMENT_FORMATS,
    MANAGER_LEVELS,
    LOYALTY_DISCOUNTS,
    DEFAULT_LESSONS,
    DEFAULT_TEMPLATES,
    LOCAL_TZ_OFFSET,
    LOCAL_TZ_LABEL,
    AVAILABLE_TIMEZONES,
    MSK_OFFSET,
)


# ─── CHANNEL_CATEGORIES ───────────────────────────────────────────────────────

class TestChannelCategories:
    def test_is_non_empty_dict(self):
        assert isinstance(CHANNEL_CATEGORIES, dict)
        assert len(CHANNEL_CATEGORIES) > 0

    def test_all_entries_have_name_and_cpm(self):
        for key, info in CHANNEL_CATEGORIES.items():
            assert "name" in info, f"Category '{key}' missing 'name'"
            assert "cpm" in info, f"Category '{key}' missing 'cpm'"

    def test_cpm_values_are_positive_integers(self):
        for key, info in CHANNEL_CATEGORIES.items():
            cpm = info["cpm"]
            assert isinstance(cpm, int) and cpm > 0, f"Category '{key}' has invalid CPM: {cpm}"

    def test_names_are_non_empty_strings(self):
        for key, info in CHANNEL_CATEGORIES.items():
            assert isinstance(info["name"], str) and info["name"], f"Category '{key}' has empty name"

    def test_keys_are_lowercase_strings(self):
        for key in CHANNEL_CATEGORIES:
            assert key == key.lower(), f"Category key '{key}' is not lowercase"

    def test_known_categories_present(self):
        for expected in ("real_estate", "humor", "crypto", "marketing", "other"):
            assert expected in CHANNEL_CATEGORIES, f"Expected category '{expected}' not found"

    def test_cpm_range_is_reasonable(self):
        for key, info in CHANNEL_CATEGORIES.items():
            cpm = info["cpm"]
            assert 100 <= cpm <= 20000, f"CPM for '{key}' is outside reasonable range: {cpm}"

    def test_real_estate_has_highest_cpm(self):
        max_cpm_key = max(CHANNEL_CATEGORIES, key=lambda k: CHANNEL_CATEGORIES[k]["cpm"])
        assert max_cpm_key == "real_estate"

    def test_other_category_exists_as_fallback(self):
        assert "other" in CHANNEL_CATEGORIES

    def test_no_duplicate_names(self):
        names = [info["name"] for info in CHANNEL_CATEGORIES.values()]
        assert len(names) == len(set(names)), "Duplicate category names found"


# ─── PLACEMENT_FORMATS ────────────────────────────────────────────────────────

class TestPlacementFormats:
    def test_all_four_formats_defined(self):
        expected = {"1/24", "1/48", "2/48", "native"}
        assert set(PLACEMENT_FORMATS.keys()) == expected

    def test_all_formats_have_required_keys(self):
        for fmt_key, fmt_info in PLACEMENT_FORMATS.items():
            for key in ("name", "hours", "description"):
                assert key in fmt_info, f"Format '{fmt_key}' missing key '{key}'"

    def test_hours_are_non_negative_integers(self):
        for fmt_key, fmt_info in PLACEMENT_FORMATS.items():
            assert isinstance(fmt_info["hours"], int) and fmt_info["hours"] >= 0

    def test_native_format_has_zero_hours(self):
        assert PLACEMENT_FORMATS["native"]["hours"] == 0

    def test_24h_format_has_24_hours(self):
        assert PLACEMENT_FORMATS["1/24"]["hours"] == 24

    def test_48h_formats_have_48_hours(self):
        assert PLACEMENT_FORMATS["1/48"]["hours"] == 48
        assert PLACEMENT_FORMATS["2/48"]["hours"] == 48

    def test_names_match_keys(self):
        assert PLACEMENT_FORMATS["1/24"]["name"] == "1/24"
        assert PLACEMENT_FORMATS["1/48"]["name"] == "1/48"
        assert PLACEMENT_FORMATS["2/48"]["name"] == "2/48"

    def test_descriptions_are_non_empty(self):
        for fmt_key, fmt_info in PLACEMENT_FORMATS.items():
            assert fmt_info["description"], f"Format '{fmt_key}' has empty description"


# ─── LOYALTY_DISCOUNTS ────────────────────────────────────────────────────────

class TestLoyaltyDiscounts:
    def test_is_non_empty_dict(self):
        assert isinstance(LOYALTY_DISCOUNTS, dict)
        assert len(LOYALTY_DISCOUNTS) > 0

    def test_keys_are_positive_integers(self):
        for k in LOYALTY_DISCOUNTS:
            assert isinstance(k, int) and k > 0

    def test_values_are_positive_percentages(self):
        for k, v in LOYALTY_DISCOUNTS.items():
            assert isinstance(v, (int, float)) and 0 < v <= 100, (
                f"Discount at order {k} is invalid: {v}"
            )

    def test_discounts_increase_with_order_count(self):
        sorted_items = sorted(LOYALTY_DISCOUNTS.items())
        for i in range(1, len(sorted_items)):
            prev_discount = sorted_items[i - 1][1]
            curr_discount = sorted_items[i][1]
            assert curr_discount >= prev_discount, (
                f"Discount did not increase: {sorted_items[i-1]} → {sorted_items[i]}"
            )

    def test_known_milestones_present(self):
        assert 2 in LOYALTY_DISCOUNTS
        assert 5 in LOYALTY_DISCOUNTS
        assert 10 in LOYALTY_DISCOUNTS

    def test_second_order_gets_5_percent(self):
        assert LOYALTY_DISCOUNTS[2] == 5

    def test_tenth_order_gets_15_percent(self):
        assert LOYALTY_DISCOUNTS[10] == 15


# ─── DEFAULT_LESSONS ─────────────────────────────────────────────────────────

class TestDefaultLessons:
    def test_is_non_empty_list(self):
        assert isinstance(DEFAULT_LESSONS, list)
        assert len(DEFAULT_LESSONS) > 0

    def test_all_lessons_have_required_keys(self):
        for lesson in DEFAULT_LESSONS:
            for key in ("id", "title", "content", "quiz"):
                assert key in lesson, f"Lesson missing key '{key}': {lesson}"

    def test_lesson_ids_are_unique(self):
        ids = [l["id"] for l in DEFAULT_LESSONS]
        assert len(ids) == len(set(ids))

    def test_lesson_ids_are_positive_integers(self):
        for lesson in DEFAULT_LESSONS:
            assert isinstance(lesson["id"], int) and lesson["id"] > 0

    def test_quiz_is_list(self):
        for lesson in DEFAULT_LESSONS:
            assert isinstance(lesson["quiz"], list)

    def test_quiz_questions_have_required_keys(self):
        for lesson in DEFAULT_LESSONS:
            for q in lesson["quiz"]:
                for key in ("question", "options", "correct"):
                    assert key in q, f"Quiz question missing key '{key}'"

    def test_correct_index_within_options_range(self):
        for lesson in DEFAULT_LESSONS:
            for q in lesson["quiz"]:
                assert 0 <= q["correct"] < len(q["options"]), (
                    f"Correct answer index {q['correct']} out of range for options {q['options']}"
                )

    def test_titles_are_non_empty(self):
        for lesson in DEFAULT_LESSONS:
            assert isinstance(lesson["title"], str) and lesson["title"]

    def test_content_is_non_empty(self):
        for lesson in DEFAULT_LESSONS:
            assert isinstance(lesson["content"], str) and lesson["content"]


# ─── DEFAULT_TEMPLATES ───────────────────────────────────────────────────────

class TestDefaultTemplates:
    def test_is_non_empty_list(self):
        assert isinstance(DEFAULT_TEMPLATES, list)
        assert len(DEFAULT_TEMPLATES) > 0

    def test_all_templates_have_name_and_text(self):
        for tmpl in DEFAULT_TEMPLATES:
            assert "name" in tmpl
            assert "text" in tmpl

    def test_names_are_non_empty_strings(self):
        for tmpl in DEFAULT_TEMPLATES:
            assert isinstance(tmpl["name"], str) and tmpl["name"]

    def test_text_is_non_empty_string(self):
        for tmpl in DEFAULT_TEMPLATES:
            assert isinstance(tmpl["text"], str) and tmpl["text"]

    def test_template_names_are_unique(self):
        names = [t["name"] for t in DEFAULT_TEMPLATES]
        assert len(names) == len(set(names))


# ─── Timezone config ─────────────────────────────────────────────────────────

class TestTimezoneConfig:
    def test_local_tz_offset_is_timedelta(self):
        assert isinstance(LOCAL_TZ_OFFSET, timedelta)

    def test_msk_offset_alias_equals_local_tz_offset(self):
        assert MSK_OFFSET == LOCAL_TZ_OFFSET

    def test_local_tz_label_starts_with_utc(self):
        assert LOCAL_TZ_LABEL.startswith("UTC")

    def test_available_timezones_is_non_empty_list(self):
        assert isinstance(AVAILABLE_TIMEZONES, list)
        assert len(AVAILABLE_TIMEZONES) > 0

    def test_available_timezones_are_tuples_with_offset_and_label(self):
        for entry in AVAILABLE_TIMEZONES:
            assert len(entry) == 2
            offset, label = entry
            assert isinstance(offset, int)
            assert isinstance(label, str)

    def test_available_timezone_offsets_are_in_reasonable_range(self):
        for offset, _ in AVAILABLE_TIMEZONES:
            assert -12 <= offset <= 14, f"Offset {offset} is outside valid timezone range"

    def test_available_timezone_offsets_are_unique(self):
        offsets = [e[0] for e in AVAILABLE_TIMEZONES]
        assert len(offsets) == len(set(offsets))

    def test_moscow_timezone_present(self):
        # Moscow is UTC+3
        offsets = [e[0] for e in AVAILABLE_TIMEZONES]
        assert 3 in offsets

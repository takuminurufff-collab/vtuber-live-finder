"""Regression tests for timestamps read from persisted channel and state JSON."""
import unittest
from datetime import datetime, timezone, timedelta

import finder


class PersistedTimestampTests(unittest.TestCase):
    def test_invalid_values_are_unknown(self):
        for value in (None, "", "not-a-date", "2026-99-99T00:00:00Z", 42, {}, []):
            with self.subTest(value=value):
                self.assertIsNone(finder.parse_iso(value))

    def test_utc_z_suffix(self):
        self.assertEqual(finder.parse_iso("2026-11-10T01:02:03Z"),
                         datetime(2026, 11, 10, 1, 2, 3, tzinfo=timezone.utc))

    def test_naive_legacy_timestamp_is_utc_aware(self):
        self.assertEqual(finder.parse_iso("2026-11-10T01:02:03"),
                         datetime(2026, 11, 10, 1, 2, 3, tzinfo=timezone.utc))

    def test_offset_is_preserved(self):
        self.assertEqual(finder.parse_iso("2026-11-10T10:02:03+09:00"),
                         datetime(2026, 11, 10, 10, 2, 3,
                                  tzinfo=timezone(timedelta(hours=9))))

    def test_bad_last_run_does_not_disable_budget_pacing(self):
        state = {"last_run": "broken", "quota": {
            "day": finder.NOW.astimezone(finder.PT).date().isoformat(), "used": 0}}
        budget = finder.Budget(state)
        self.assertGreaterEqual(budget.run_cap, 0)
        self.assertLessEqual(budget.run_cap, finder.DAILY_BUDGET)


if __name__ == "__main__":
    unittest.main()

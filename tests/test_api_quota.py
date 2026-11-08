"""Offline regression tests for YouTube API quota accounting."""
import unittest
from unittest.mock import Mock, patch

import finder


class FakeBudget:
    def __init__(self, remaining=10):
        self.q = {"used": 0}
        self.run_used = 0
        self.remaining = remaining

    def day_left(self):
        return self.remaining - self.q["used"]

    def spend(self, amount):
        self.q["used"] += amount
        self.run_used += amount


class ApiQuotaTests(unittest.TestCase):
    def setUp(self):
        self.old_budget = finder.budget
        finder.budget = FakeBudget()

    def tearDown(self):
        finder.budget = self.old_budget

    def test_missing_playlist_still_consumes_one_unit(self):
        response = Mock(status_code=404)
        with patch.object(finder.session, "get", return_value=response) as request:
            self.assertIsNone(finder.playlist_total("UULFexample"))
        request.assert_called_once()
        self.assertEqual(finder.budget.run_used, 1)
        self.assertEqual(finder.budget.q["used"], 1)

    def test_quota_exceeded_response_accounts_for_attempt(self):
        response = Mock(status_code=403)
        response.json.return_value = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
        with patch.object(finder.session, "get", return_value=response):
            with self.assertRaises(finder.QuotaExceeded):
                finder.api_get("playlistItems", {"playlistId": "test"}, cost=1)
        self.assertEqual(finder.budget.run_used, 1)
        self.assertEqual(finder.budget.q["used"], finder.DAILY_BUDGET)

    def test_no_request_when_daily_budget_is_exhausted(self):
        finder.budget = FakeBudget(remaining=0)
        with patch.object(finder.session, "get") as request:
            with self.assertRaises(finder.QuotaExceeded):
                finder.api_get("playlistItems", {"playlistId": "test"}, cost=1)
        request.assert_not_called()
        self.assertEqual(finder.budget.run_used, 0)


if __name__ == "__main__":
    unittest.main()

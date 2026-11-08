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

    def run_left(self):
        return self.run_limit - self.run_used if hasattr(self, "run_limit") else self.day_left()

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

    def test_no_request_when_run_budget_is_exhausted(self):
        finder.budget = FakeBudget(remaining=100)
        finder.budget.run_limit = 0
        with patch.object(finder.session, "get") as request:
            with self.assertRaises(finder.QuotaExceeded):
                finder.api_get("search", {"q": "VTuber"}, cost=100)
        request.assert_not_called()
        self.assertEqual(finder.budget.q["used"], 0)

    def test_expensive_request_is_blocked_before_run_cap_is_exceeded(self):
        finder.budget = FakeBudget(remaining=1000)
        finder.budget.run_limit = 99
        with patch.object(finder.session, "get") as request:
            with self.assertRaises(finder.QuotaExceeded):
                finder.api_get("search", {"q": "VTuber"}, cost=100)
        request.assert_not_called()

    def test_request_can_use_the_last_available_run_unit(self):
        finder.budget = FakeBudget(remaining=100)
        finder.budget.run_limit = 1
        response = Mock(status_code=200)
        response.json.return_value = {"items": []}
        with patch.object(finder.session, "get", return_value=response) as request:
            finder.api_get("channels", {"id": "UCexample"}, cost=1)
        request.assert_called_once()
        self.assertEqual(finder.budget.run_left(), 0)
        self.assertEqual(finder.budget.q["used"], 1)

    def test_missing_only_refresh_uses_one_last_unit_without_overrun(self):
        # Integration check: refresh_due_counts -> playlist_total -> api_get.
        finder.budget = FakeBudget(remaining=100)
        finder.budget.run_limit = 1
        cid = "UC" + "a" * 22
        rec = {"videos": 24, "shorts": None, "lives": 3}
        response = Mock(status_code=200)
        response.json.return_value = {"pageInfo": {"totalResults": 8}}
        metrics = {}
        with patch.object(finder.session, "get", return_value=response) as request:
            self.assertEqual(finder.refresh_due_counts({cid: rec}, metrics=metrics), 1)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["params"]["playlistId"],
                         "UUSH" + cid[2:])
        self.assertEqual((rec["videos"], rec["shorts"], rec["lives"]), (24, 8, 3))
        self.assertEqual(finder.budget.run_used, 1)
        self.assertEqual(finder.budget.run_left(), 0)
        self.assertEqual(metrics["quota_units"], 1)
        self.assertEqual(metrics["resolved"], 1)

    def test_no_request_when_daily_budget_is_exhausted(self):
        finder.budget = FakeBudget(remaining=0)
        with patch.object(finder.session, "get") as request:
            with self.assertRaises(finder.QuotaExceeded):
                finder.api_get("playlistItems", {"playlistId": "test"}, cost=1)
        request.assert_not_called()
        self.assertEqual(finder.budget.run_used, 0)


    def test_count_refresh_tracks_units_when_second_playlist_fails(self):
        finder.budget = FakeBudget()
        finder.budget.run_left = finder.budget.day_left
        db = {"UC" + "a" * 22: {"counts_at": None}}
        metrics = {}
        responses = [
            Mock(status_code=200),
            Mock(status_code=500, text="server error"),
        ]
        responses[0].json.return_value = {"pageInfo": {"totalResults": 5}}
        responses[1].json.return_value = {"error": {"errors": [{"reason": "backendError"}]}}
        with patch.object(finder.session, "get", side_effect=responses) as request:
            with self.assertRaises(RuntimeError):
                finder.refresh_due_counts(db, metrics=metrics)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(metrics["quota_units"], 2)
        self.assertEqual(finder.budget.run_used, 2)
        self.assertNotIn("videos", db[next(iter(db))])
        self.assertNotIn("counts_at", {k: v for k, v in db[next(iter(db))].items() if v is not None})

if __name__ == "__main__":
    unittest.main()

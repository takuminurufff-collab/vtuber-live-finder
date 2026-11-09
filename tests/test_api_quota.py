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

    def test_failed_run_checkpoint_saves_quota_but_not_unsaved_sweep_progress(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            previous = {"last_run": "2026-11-07T00:00:00Z",
                        "sweep": {"#vtuber": "2026-11-07T00:00:00Z"},
                        "quota": {"day": "2026-11-07", "used": 25}}
            with open(path, "w", encoding="utf-8") as output:
                json.dump(previous, output)
            finder.budget = FakeBudget(remaining=300)
            finder.budget.q = {"day": "2026-11-07", "used": 125}
            with patch.object(finder, "STATE_PATH", path):
                finder.save_failed_run_quota()
                with open(path, encoding="utf-8") as source:
                    checkpoint = json.load(source)
            self.assertEqual(checkpoint["quota"]["used"], 125)
            self.assertEqual(checkpoint["sweep"], previous["sweep"])
            self.assertEqual(checkpoint["last_run"], previous["last_run"])
            self.assertFalse(os.path.exists(path + ".quota-tmp"))

    def test_main_network_failure_saves_quota_and_propagates_error(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.json")
            db_path = os.path.join(directory, "channels.json")
            before = {"quota": {"day": finder.NOW.astimezone(finder.PT).date().isoformat(), "used": 0},
                      "last_run": finder.iso(finder.NOW - finder.timedelta(hours=1))}
            with open(state_path, "w", encoding="utf-8") as output:
                json.dump(before, output)
            with open(db_path, "w", encoding="utf-8") as output:
                json.dump({}, output)
            old = finder.budget
            try:
                def failed_refresh(db, *args, **kwargs):
                    finder.budget.spend(2)
                    raise finder.requests.Timeout("API timed out")
                with patch.object(finder, "STATE_PATH", state_path), \
                     patch.object(finder, "DB_PATH", db_path), \
                     patch.object(finder, "refresh_due_counts", side_effect=failed_refresh):
                    with self.assertRaises(finder.requests.Timeout):
                        finder.main()
                with open(state_path, encoding="utf-8") as source:
                    saved = json.load(source)
                with open(db_path, encoding="utf-8") as source:
                    db_saved = json.load(source)
                self.assertEqual(saved["quota"]["used"], 2)
                self.assertEqual(saved["last_run"], before["last_run"])
                self.assertEqual(db_saved, {})
            finally:
                finder.budget = old

    def test_timeout_is_accounted_as_possible_api_usage(self):
        # The server may have accepted the request despite a client timeout.
        finder.budget = FakeBudget(remaining=500)
        finder.budget.run_limit = 100
        with patch.object(finder.session, "get", side_effect=finder.requests.Timeout("timeout")) as request:
            with self.assertRaises(finder.requests.Timeout):
                finder.api_get("search", {"q": "Vtuber"}, cost=100)
        request.assert_called_once()
        self.assertEqual(finder.budget.run_used, 100)
        self.assertEqual(finder.budget.q["used"], 100)
        self.assertEqual(finder.budget.run_left(), 0)

    def test_connection_failure_conservatively_tracks_attempted_unit(self):
        finder.budget = FakeBudget(remaining=30)
        finder.budget.run_limit = 1
        with patch.object(finder.session, "get", side_effect=finder.requests.ConnectionError("disconnected")):
            with self.assertRaises(finder.requests.ConnectionError):
                finder.api_get("playlistItems", {"playlistId": "UULFexample"}, cost=1)
        self.assertEqual(finder.budget.run_used, 1)

    def test_preflight_rejection_does_not_charge_failed_network_attempt(self):
        finder.budget = FakeBudget(remaining=500)
        finder.budget.run_limit = 99
        with patch.object(finder.session, "get") as request:
            with self.assertRaises(finder.QuotaExceeded):
                finder.api_get("search", {"q": "Vtuber"}, cost=100)
        request.assert_not_called()
        self.assertEqual(finder.budget.run_used, 0)

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


class BudgetPacingTests(unittest.TestCase):
    def test_five_minute_followup_does_not_get_hourly_allowance(self):
        now = finder.NOW
        state = {"last_run": finder.iso(now - finder.timedelta(minutes=5)),
                 "quota": {"day": now.astimezone(finder.PT).date().isoformat(), "used": 0}}
        budget = finder.Budget(state)
        reset = finder.datetime.combine(now.astimezone(finder.PT).date() + finder.timedelta(days=1),
                                        finder.datetime.min.time(), finder.PT)
        hours_left = max((reset - now).total_seconds() / 3600, 1)
        expected = int(finder.DAILY_BUDGET * min(1, (5 / 60) / hours_left))
        self.assertLessEqual(budget.run_cap, expected + 1)

    def test_hourly_run_still_receives_normal_allowance(self):
        now = finder.NOW
        state = {"last_run": finder.iso(now - finder.timedelta(hours=1)),
                 "quota": {"day": now.astimezone(finder.PT).date().isoformat(), "used": 0}}
        budget = finder.Budget(state)
        self.assertGreater(budget.run_cap, 0)


class PersistentDataSafetyTests(unittest.TestCase):
    def test_missing_json_uses_default(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(finder.load(os.path.join(directory, "missing.json"), {"ok": True}), {"ok": True})

    def test_corrupt_json_does_not_get_replaced_with_empty_data(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "channels.json")
            original = '{"UCabc":'
            with open(path, "w", encoding="utf-8") as output:
                output.write(original)
            with self.assertRaisesRegex(ValueError, "破損"):
                finder.load(path, {})
            with open(path, encoding="utf-8") as source:
                self.assertEqual(source.read(), original)

    def test_failed_run_checkpoint_refuses_to_overwrite_corrupt_state(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            original = '{broken json'
            with open(path, "w", encoding="utf-8") as output:
                output.write(original)
            previous_budget = finder.budget
            try:
                finder.budget = FakeBudget()
                with patch.object(finder, "STATE_PATH", path):
                    with self.assertRaises(ValueError):
                        finder.save_failed_run_quota()
                with open(path, encoding="utf-8") as source:
                    self.assertEqual(source.read(), original)
            finally:
                finder.budget = previous_budget


class BudgetConfigurationSafetyTests(unittest.TestCase):
    def test_budget_configuration_never_exceeds_approved_daily_limit(self):
        import importlib.util
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"DAILY_BUDGET": "999999"}):
            spec = importlib.util.spec_from_file_location("finder_budget_guard_test", finder.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.DAILY_BUDGET, 9300)

    def test_lower_configured_budget_is_preserved(self):
        import importlib.util
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"DAILY_BUDGET": "1200"}):
            spec = importlib.util.spec_from_file_location("finder_budget_guard_low_test", finder.__file__)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(module.DAILY_BUDGET, 1200)

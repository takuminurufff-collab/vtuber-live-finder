"""APIへの通信なしで再検査処理を確認するテスト。

実行: python -m unittest discover -s tests -v
"""
import unittest
from unittest.mock import patch
import finder


def make_channel(name, description, subscribers=12, hidden=False):
    return {
        "snippet": {"title": name, "description": description, "thumbnails": {}},
        "statistics": {"subscriberCount": str(subscribers), "hiddenSubscriberCount": hidden},
    }


class FakeBudget:
    def __init__(self, remaining=100):
        self.remaining = remaining
        self.run_used = 0

    def run_left(self):
        return self.remaining

    def day_left(self):
        return self.remaining


class RecheckTests(unittest.TestCase):
    def setUp(self):
        self.old_budget = finder.budget
        finder.budget = FakeBudget()

    def tearDown(self):
        finder.budget = self.old_budget

    def test_count_quota_metrics_include_successful_requests(self):
        rec = {"videos": None, "shorts": None, "lives": None}
        metrics = {}
        def refresh(_cid, record):
            finder.budget.run_used += 3
            record.update(videos=1, shorts=2, lives=3,
                          counts_at=finder.iso(finder.NOW))
        with patch.object(finder, "refresh_counts", side_effect=refresh):
            self.assertEqual(finder.refresh_due_counts({"UC123": rec}, metrics=metrics), 1)
        self.assertEqual(metrics["quota_units"], 3)
        self.assertEqual(metrics["complete"], 1)

    def test_count_quota_metrics_include_failed_requests(self):
        rec = {"videos": None, "shorts": None, "lives": None}
        metrics = {}
        def fail(_cid, _rec):
            finder.budget.run_used += 2
            raise finder.QuotaExceeded()
        with patch.object(finder, "refresh_counts", side_effect=fail):
            with self.assertRaises(finder.QuotaExceeded):
                finder.refresh_due_counts({"UC123": rec}, metrics=metrics)
        self.assertEqual(metrics["quota_units"], 2)
        self.assertEqual(metrics.get("complete", 0), 0)

    def test_unavailable_playlist_is_unknown_not_zero(self):
        with patch.object(finder, "api_get", return_value=None):
            self.assertIsNone(finder.playlist_total("UUSHexample"))
        with patch.object(finder, "api_get", return_value={"pageInfo": {"totalResults": 0}}):
            self.assertEqual(finder.playlist_total("UUSHexample"), 0)

    def test_missing_playlist_count_remains_due_for_retry(self):
        rec = {"videos": 4, "shorts": None, "lives": 0,
               "counts_at": finder.iso(finder.NOW)}
        self.assertTrue(finder.counts_due(rec))

    def test_unavailable_playlist_does_not_erase_known_count(self):
        rec = {"videos": 12, "shorts": 7, "lives": 2,
               "counts_at": "2026-10-01T00:00:00Z"}
        with patch.object(finder, "playlist_total", side_effect=[None, 9, 3]):
            finder.refresh_counts("UC123", rec)
        self.assertEqual((rec["videos"], rec["shorts"], rec["lives"]), (12, 9, 3))
        self.assertEqual(rec["counts_at"], "2026-10-01T00:00:00Z")
        self.assertEqual(rec["counts_attempt_at"], finder.iso(finder.NOW))
        self.assertFalse(finder.counts_due(rec))

    def test_known_counts_partial_failure_retries_after_cooldown(self):
        old = finder.iso(finder.NOW - finder.timedelta(days=5))
        rec = {"videos": 12, "shorts": 7, "lives": 2, "counts_at": old}
        with patch.object(finder, "playlist_total", side_effect=[None, 8, 3]):
            finder.refresh_counts("UC123", rec)
        self.assertEqual(rec["counts_at"], old)
        self.assertEqual((rec["videos"], rec["shorts"], rec["lives"]), (12, 8, 3))
        self.assertFalse(finder.counts_due(rec))
        rec["counts_attempt_at"] = finder.iso(finder.NOW - finder.timedelta(days=1, minutes=1))
        self.assertTrue(finder.counts_due(rec))

    def test_all_playlist_counts_success_refreshes_timestamp(self):
        rec = {"videos": 12, "shorts": 7, "lives": 2,
               "counts_at": finder.iso(finder.NOW - finder.timedelta(days=5))}
        with patch.object(finder, "playlist_total", side_effect=[14, 9, 4]):
            finder.refresh_counts("UC123", rec)
        self.assertEqual(rec["counts_at"], finder.iso(finder.NOW))
        self.assertFalse(finder.counts_due(rec))

    def test_missing_playlist_keeps_backlog_due(self):
        rec = {"videos": None, "shorts": 7, "lives": 2,
               "counts_at": None}
        with patch.object(finder, "playlist_total", side_effect=[None, 9, 3]):
            finder.refresh_counts("UC123", rec)
        self.assertIsNone(rec["videos"])
        self.assertIsNone(rec["counts_at"])
        self.assertEqual(rec["counts_attempt_at"], finder.iso(finder.NOW))
        self.assertFalse(finder.counts_due(rec))  # 24時間の再試行待ち

    def test_missing_playlist_retries_after_one_day_not_every_hour(self):
        rec = {"videos": None, "shorts": 5, "lives": 2, "counts_at": None}
        with patch.object(finder, "playlist_total", side_effect=[None, 5, 2]):
            finder.refresh_counts("UC123", rec)
        self.assertFalse(finder.counts_due(rec))
        rec["counts_attempt_at"] = finder.iso(
            finder.NOW - finder.timedelta(hours=23))
        self.assertFalse(finder.counts_due(rec))
        rec["counts_attempt_at"] = finder.iso(
            finder.NOW - finder.timedelta(days=1, minutes=1))
        self.assertTrue(finder.counts_due(rec))

    def test_missing_playlist_retry_does_not_block_other_channels(self):
        db = {
            "UC_COOLDOWN": {"videos": None, "shorts": 2, "lives": 1,
                            "counts_attempt_at": finder.iso(finder.NOW)},
            "UC_NEW": {"videos": None, "shorts": None, "lives": None},
        }
        with patch.object(finder, "refresh_counts") as refresh:
            result = finder.refresh_due_counts(db, max_channels=1)
        self.assertEqual(result, 1)
        refresh.assert_called_once_with("UC_NEW", db["UC_NEW"])

    def test_non_live_first_attempt_precedes_live_retry(self):
        db = {
            "UC_LIVE_RETRY": {"videos": None, "shorts": 3, "lives": 1,
                              "is_live": True,
                              "counts_attempt_at": finder.iso(finder.NOW - finder.timedelta(days=2))},
            "UC_NEW_OFFLINE": {"videos": None, "shorts": None, "lives": None,
                               "is_live": False},
        }
        with patch.object(finder, "refresh_counts") as refresh:
            self.assertEqual(finder.refresh_due_counts(db, max_channels=1), 1)
        refresh.assert_called_once_with("UC_NEW_OFFLINE", db["UC_NEW_OFFLINE"])

    def test_live_first_attempt_precedes_non_live_first_attempt(self):
        db = {
            "UC_OFFLINE": {"videos": None, "shorts": None, "lives": None,
                           "is_live": False},
            "UC_LIVE": {"videos": None, "shorts": None, "lives": None,
                        "is_live": True},
        }
        with patch.object(finder, "refresh_counts") as refresh:
            self.assertEqual(finder.refresh_due_counts(db, max_channels=1), 1)
        refresh.assert_called_once_with("UC_LIVE", db["UC_LIVE"])

    def test_count_refresh_is_atomic_on_quota_exception(self):
        previous = {"videos": 4, "shorts": 5, "lives": 6,
                    "counts_at": "2026-10-01T00:00:00Z"}
        before = previous.copy()
        def get_total(playlist_id):
            if playlist_id.startswith("UULV"):
                raise finder.QuotaExceeded()
            return 99
        with patch.object(finder, "playlist_total", side_effect=get_total):
            with self.assertRaises(finder.QuotaExceeded):
                finder.refresh_counts("UC123", previous)
        self.assertEqual(previous, before)

    def test_count_refresh_is_atomic_on_http_error(self):
        previous = {"videos": None, "shorts": None, "lives": None, "counts_at": None}
        with patch.object(finder, "playlist_total", side_effect=[20, RuntimeError("API failed")]):
            with self.assertRaises(RuntimeError):
                finder.refresh_counts("UC123", previous)
        self.assertEqual(previous, {"videos": None, "shorts": None, "lives": None, "counts_at": None})

    def test_successful_count_refresh_updates_all_values_together(self):
        rec = {"videos": None, "shorts": None, "lives": None, "counts_at": None}
        with patch.object(finder, "playlist_total", side_effect=[0, 19, 2]) as fetch:
            finder.refresh_counts("UC123", rec)
        self.assertEqual([rec[k] for k in ("videos", "shorts", "lives")], [0, 19, 2])
        self.assertEqual(rec["counts_at"], finder.iso(finder.NOW))
        self.assertEqual(fetch.call_count, 3)

    def test_count_metrics_distinguish_full_and_partial_success(self):
        db = {
            "UCfirst": {"videos": None, "shorts": None, "lives": None},
            "UCsecond": {"videos": None, "shorts": None, "lives": None},
        }
        metrics = {}
        finder.budget = FakeBudget(remaining=100)
        with patch.object(finder, "playlist_total", side_effect=[1, 2, 3, None, 4, 5]):
            self.assertEqual(finder.refresh_due_counts(db, metrics=metrics), 2)
        self.assertEqual(metrics, {"quota_units": 0, "attempted": 2, "complete": 1,
                                   "partial": 1, "resolved": 1})

    def test_count_metrics_do_not_report_api_exception_as_success(self):
        db = {"UCfirst": {"videos": None, "shorts": None, "lives": None}}
        metrics = {}
        finder.budget = FakeBudget(remaining=100)
        with patch.object(finder, "playlist_total", side_effect=RuntimeError("API failed")):
            with self.assertRaises(RuntimeError):
                finder.refresh_due_counts(db, metrics=metrics)
        self.assertEqual(metrics, {"quota_units": 0})

    def test_backlog_distinguishes_eligible_and_retry_cooldown(self):
        now = finder.NOW
        db = {
            "UC_FIRST": {"videos": None, "shorts": 0, "lives": 0},
            "UC_WAIT": {"videos": None, "shorts": 0, "lives": 0,
                        "counts_attempt_at": finder.iso(now - finder.timedelta(hours=1))},
            "UC_RETRY": {"videos": 3, "shorts": None, "lives": 0,
                         "counts_attempt_at": finder.iso(now - finder.timedelta(hours=25))},
            "UC_DONE": {"videos": 3, "shorts": 0, "lives": 0,
                        "counts_at": finder.iso(now)},
        }
        self.assertEqual(finder.counts_backlog(db), (3, 2, 1))

    def test_backlog_does_not_consume_api_budget(self):
        finder.budget = FakeBudget(remaining=0)
        db = {"UC_NEW": {"videos": None, "shorts": None, "lives": None}}
        with patch.object(finder, "playlist_total", side_effect=AssertionError("API called")):
            self.assertEqual(finder.counts_backlog(db), (1, 1, 0))
        self.assertEqual(finder.budget.run_left(), 0)

    def test_missing_counts_get_priority_over_stale_complete_counts(self):
        db = {
            "UC_STALE": {"videos": 1, "shorts": 2, "lives": 3,
                         "counts_at": finder.iso(finder.NOW - finder.timedelta(days=10))},
            "UC_MISSING": {"videos": None, "shorts": None, "lives": None,
                           "counts_at": None},
        }
        with patch.object(finder, "refresh_counts") as refresh:
            result = finder.refresh_due_counts(db, max_channels=1)
        self.assertEqual(result, 1)
        self.assertEqual(refresh.call_args.args[0], "UC_MISSING")

    def test_count_refresh_does_not_exceed_batch_limit(self):
        db = {str(i): {"counts_at": None, "videos": None, "shorts": None,
                       "lives": None} for i in range(100)}
        with patch.object(finder, "refresh_counts") as refresh:
            result = finder.refresh_due_counts(db, max_channels=20)
        self.assertEqual(result, 20)
        self.assertEqual(refresh.call_count, 20)

    def test_never_attempted_missing_counts_precede_retry(self):
        finder.budget = FakeBudget(remaining=100)
        old = finder.iso(finder.NOW - finder.timedelta(days=2))
        db = {
            "UC_RETRY": {"videos": None, "shorts": None, "lives": None,
                         "counts_at": None, "counts_attempt_at": old},
            "UC_NEW": {"videos": None, "shorts": None, "lives": None,
                       "counts_at": None},
        }
        with patch.object(finder, "refresh_counts") as refresh:
            finder.refresh_due_counts(db, max_channels=1)
        refresh.assert_called_once_with("UC_NEW", db["UC_NEW"])

    def test_oldest_failed_count_attempt_precedes_recent_retry(self):
        finder.budget = FakeBudget(remaining=100)
        older = finder.iso(finder.NOW - finder.timedelta(days=4))
        recent = finder.iso(finder.NOW - finder.timedelta(days=2))
        db = {
            "UC_RECENT": {"videos": None, "shorts": None, "lives": None,
                          "counts_at": None, "counts_attempt_at": recent},
            "UC_OLDER": {"videos": None, "shorts": None, "lives": None,
                         "counts_at": None, "counts_attempt_at": older},
        }
        with patch.object(finder, "refresh_counts") as refresh:
            finder.refresh_due_counts(db, max_channels=1)
        refresh.assert_called_once_with("UC_OLDER", db["UC_OLDER"])

    def test_count_refresh_respects_low_api_budget(self):
        finder.budget = FakeBudget(remaining=2)
        db = {"UC": {"counts_at": None, "videos": None, "shorts": None,
                     "lives": None}}
        with patch.object(finder, "refresh_counts") as refresh:
            result = finder.refresh_due_counts(db, max_channels=20)
        self.assertEqual(result, 0)
        refresh.assert_not_called()

    def test_missing_fields_are_due_even_with_recent_timestamp(self):
        row = {"videos": 12, "shorts": None, "lives": 3,
               "counts_at": finder.iso(finder.NOW)}
        self.assertTrue(finder.counts_due(row))

    def test_recovers_corporate_vtuber(self):
        db, state = {}, {}
        rejected = {"UC_TEST": finder.iso(finder.NOW)}
        stats = {"recovered": 0}
        channel = make_channel("新人VTuber", "ホロライブ所属")
        with patch.object(finder, "fetch_channels", return_value={"UC_TEST": channel}) as fetch:
            finder.recheck_rejected(db, state, rejected, stats)
        fetch.assert_called_once_with(["UC_TEST"])
        self.assertIn("UC_TEST", db)
        self.assertNotIn("UC_TEST", rejected)
        self.assertEqual(stats["recovered"], 1)

    def test_unverified_live_is_unknown_then_eventually_offline(self):
        rec = {"live": {"video_id": "abc12345678", "title": "Stream"},
               "is_live": True}
        for n in (1, 2):
            finder.update_live_state(rec, None, {"abc12345678"}, {})
            self.assertIsNone(rec["is_live"])
            self.assertEqual(rec["live_verify_failures"], n)
            self.assertIn("live", rec)
        finder.update_live_state(rec, None, {"abc12345678"}, {})
        self.assertIs(rec["is_live"], False)
        self.assertNotIn("live", rec)
        self.assertNotIn("live_verify_failures", rec)

    def test_verified_live_resets_unknown_state(self):
        rec = {"live": {"video_id": "abc12345678"}, "is_live": None,
               "live_verify_failures": 2}
        live = {"id": "abc12345678",
                "snippet": {"title": "Still live", "thumbnails": {}},
                "liveStreamingDetails": {"concurrentViewers": "42",
                                         "actualStartTime": "2026-11-07T10:00:00Z"}}
        finder.update_live_state(rec, live, {"abc12345678"}, {"abc12345678": live})
        self.assertIs(rec["is_live"], True)
        self.assertEqual(rec["live"]["viewers"], 42)
        self.assertNotIn("live_verify_failures", rec)

    def test_confirmed_ended_stream_is_offline_immediately(self):
        rec = {"live": {"video_id": "abc12345678"}, "is_live": True}
        finder.update_live_state(rec, None, {"abc12345678"},
                                 {"abc12345678": {"snippet": {"liveBroadcastContent": "none"}}})
        self.assertIs(rec["is_live"], False)
        self.assertNotIn("live", rec)

    def test_hidden_subscribers_not_zero(self):
        db, state, rejected = {}, {}, {"UC_HIDDEN": finder.iso(finder.NOW)}
        with patch.object(finder, "fetch_channels", return_value={
            "UC_HIDDEN": make_channel("VTuber", "", hidden=True),
        }):
            finder.recheck_rejected(db, state, rejected, {"recovered": 0})
        self.assertIn("UC_HIDDEN", db)
        self.assertIsNone(db["UC_HIDDEN"]["subscribers"])
        self.assertNotIn("UC_HIDDEN", rejected)

    def test_private_or_missing_subscriber_total_is_unknown(self):
        self.assertIsNone(finder.subscriber_count({"hiddenSubscriberCount": True, "subscriberCount": "999"}))
        self.assertIsNone(finder.subscriber_count({}))
        self.assertEqual(finder.subscriber_count({"subscriberCount": "0"}), 0)
        self.assertTrue(finder.within_pool_limit(None))

    def test_known_subscriber_limit_still_applies(self):
        with patch.object(finder, "POOL_MAX_SUBSCRIBERS", 500):
            self.assertTrue(finder.within_pool_limit(None))
            self.assertTrue(finder.within_pool_limit(500))
            self.assertFalse(finder.within_pool_limit(501))

    def test_recheck_retains_channel_without_subscriber_field(self):
        db, state = {}, {}
        rejected = {"UC_MISSING": finder.iso(finder.NOW)}
        ch = make_channel("新人VTuber", "配信者")
        del ch["statistics"]["subscriberCount"]
        with patch.object(finder, "fetch_channels", return_value={"UC_MISSING": ch}):
            finder.recheck_rejected(db, state, rejected, {"recovered": 0})
        self.assertIn("UC_MISSING", db)
        self.assertIsNone(db["UC_MISSING"]["subscribers"])

    def test_low_quota_does_not_call_api(self):
        finder.budget = FakeBudget(remaining=1)
        with patch.object(finder, "fetch_channels") as fetch:
            finder.recheck_rejected({}, {}, {"UC_TEST": finder.iso(finder.NOW)}, {"recovered": 0})
        fetch.assert_not_called()

    def test_batch_is_bounded(self):
        rejected = {"UC" + str(i).zfill(22): finder.iso(finder.NOW) for i in range(60)}
        with patch.object(finder, "fetch_channels", return_value={}) as fetch:
            finder.recheck_rejected({}, {}, rejected, {"recovered": 0})
        self.assertEqual(len(fetch.call_args.args[0]), finder.RECHECK_REJECTED_PER_RUN)

    def test_sweep_uses_remaining_100_units(self):
        old = finder.NOW - finder.timedelta(hours=1)
        state = {"sweep": {tag: finder.iso(old) for tag in finder.SWEEP_TAGS}}
        finder.budget = FakeBudget(remaining=100)
        stats = {"videos": 0, "slices": 0, "overflow": 0}

        def one_search(params):
            # 検索1回の100ユニットを使い切った状態を再現。
            finder.budget.remaining -= 100
            return {"items": [], "pageInfo": {"totalResults": 0}}

        with patch.object(finder, "search_page", side_effect=one_search) as search:
            finder.sweep(state, {}, stats)
        search.assert_called_once()
        self.assertEqual(stats["slices"], 1)
        self.assertGreater(finder.parse_iso(state["sweep"][finder.SWEEP_TAGS[0]]), old)

    def test_sweep_does_not_search_with_less_than_100_units(self):
        finder.budget = FakeBudget(remaining=99)
        state = {"sweep": {}}
        with patch.object(finder, "search_page") as search:
            finder.sweep(state, {}, {"videos": 0, "slices": 0, "overflow": 0})
        search.assert_not_called()

    def test_old_discovery_cursor_is_not_discarded(self):
        # 3日以上の未検索期間でもカーソルを最新側に飛ばさない。
        old = finder.NOW - finder.timedelta(days=8)
        state = {"sweep": {tag: finder.iso(old) for tag in finder.SWEEP_TAGS}}
        finder.budget = FakeBudget(remaining=0)
        finder.sweep(state, {}, {"videos": 0, "slices": 0, "overflow": 0})
        for tag in finder.SWEEP_TAGS:
            self.assertEqual(state["sweep"][tag], finder.iso(old))

    def test_live_query_diversity_does_not_increase_calls_per_run(self):
        # キーワード数を増やしても1実行あたり検索は1回のまま。
        self.assertGreater(len(finder.LIVE_PLAN), 12)
        state = {"live_plan_index": len(finder.LIVE_PLAN) - 1}
        with patch.object(finder, "search_page", return_value={"items": []}) as search:
            finder.live_search(state, {})
        search.assert_called_once()
        self.assertEqual(state["live_plan_index"], len(finder.LIVE_PLAN))

    def test_partial_pagination_does_not_advance_cursor(self):
        old = finder.NOW - finder.timedelta(hours=2)
        state = {"sweep": {tag: finder.iso(old) for tag in finder.SWEEP_TAGS}}
        found = {}
        stats = {"videos": 0, "slices": 0, "overflow": 0}
        # どのページにも次ページがあり、上限で収集が打ち切られるケース
        finder.budget = FakeBudget(remaining=300)
        page = {"items": [], "nextPageToken": "another-page",
                "pageInfo": {"totalResults": 500}}
        with patch.object(finder, "search_page", return_value=page) as search:
            finder.sweep(state, found, stats)
        self.assertGreaterEqual(search.call_count, 1)
        self.assertGreaterEqual(stats["overflow"], 1)
        self.assertEqual(stats["slices"], 0)
        for tag in finder.SWEEP_TAGS:
            self.assertEqual(state["sweep"][tag], finder.iso(old))

    def test_pagination_resumes_from_saved_token(self):
        tag = finder.SWEEP_TAGS[0]
        old = finder.NOW - finder.timedelta(hours=2)
        state = {"sweep": {t: finder.iso(finder.NOW) for t in finder.SWEEP_TAGS}}
        state["sweep"][tag] = finder.iso(old)
        stats = {"videos": 0, "slices": 0, "overflow": 0}

        budget = FakeBudget(remaining=200)
        finder.budget = budget

        def first_search(params):
            # 最初のページ取得後に、この回のAPI枠を使い切った状態を再現
            budget.remaining = 0
            return {"items": [], "nextPageToken": "page-2",
                    "pageInfo": {"totalResults": 75}}

        with patch.object(finder, "search_page", side_effect=first_search) as search:
            finder.sweep(state, {}, stats)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(state["sweep"][tag], finder.iso(old))
        self.assertEqual(state["sweep_pending"][tag]["next_page"], "page-2")

        budget.remaining = 200
        with patch.object(finder, "search_page", return_value={
            "items": [], "pageInfo": {"totalResults": 75}
        }) as search:
            finder.sweep(state, {}, stats)
        # 保存位置から再開後は、残予算があれば次の時間帯も探索する。
        self.assertGreaterEqual(search.call_count, 1)
        self.assertEqual(search.call_args_list[0].args[0]["pageToken"], "page-2")
        self.assertNotIn(tag, state["sweep_pending"])
        self.assertGreater(finder.parse_iso(state["sweep"][tag]), old)

    def test_pagination_at_limit_is_split_without_losing_cursor(self):
        tag = finder.SWEEP_TAGS[0]
        old = finder.NOW - finder.timedelta(hours=2)
        state = {"sweep": {t: finder.iso(finder.NOW) for t in finder.SWEEP_TAGS}}
        state["sweep"][tag] = finder.iso(old)
        finder.budget = FakeBudget(remaining=10000)
        # 件数推定は400以下だが、実際には500件上限に達する場合。
        # 最初の区間から探し直して分割する必要がある。
        page = {"items": [], "nextPageToken": "overflow",
                "pageInfo": {"totalResults": 300}}
        with patch.object(finder, "search_page", return_value=page):
            finder.sweep(state, {}, {"videos": 0, "slices": 0, "overflow": 0})
        self.assertEqual(state["sweep"][tag], finder.iso(old))
        self.assertTrue(state["sweep_pending"][tag]["restart"])
        self.assertIsNone(state["sweep_pending"][tag]["next_page"])

    def test_expired_resume_token_restarts_same_window(self):
        tag = finder.SWEEP_TAGS[0]
        old = finder.iso(finder.NOW - finder.timedelta(hours=2))
        stop = finder.iso(finder.NOW - finder.timedelta(hours=1))
        state = {
            "sweep": {t: finder.iso(finder.NOW) for t in finder.SWEEP_TAGS},
            "sweep_pending": {tag: {"start": old, "stop": stop,
                                    "next_page": "expired", "pages": 2}},
        }
        state["sweep"][tag] = old
        finder.budget = FakeBudget(remaining=200)
        with patch.object(finder, "search_page",
                          side_effect=RuntimeError("search でエラー (400): invalidPageToken")):
            finder.sweep(state, {}, {"videos": 0, "slices": 0, "overflow": 0})
        self.assertEqual(state["sweep"][tag], old)
        self.assertTrue(state["sweep_pending"][tag]["restart"])
        self.assertEqual(state["sweep_pending"][tag]["stop"], stop)

    def test_token_expires_during_active_pagination(self):
        tag = finder.SWEEP_TAGS[0]
        old = finder.iso(finder.NOW - finder.timedelta(hours=2))
        state = {"sweep": {t: finder.iso(finder.NOW) for t in finder.SWEEP_TAGS}}
        state["sweep"][tag] = old
        finder.budget = FakeBudget(remaining=300)
        first = {"items": [], "nextPageToken": "expired",
                 "pageInfo": {"totalResults": 70}}
        with patch.object(finder, "search_page", side_effect=[
                first, RuntimeError("search でエラー (400): invalidPageToken")]):
            finder.sweep(state, {}, {"videos": 0, "slices": 0, "overflow": 0})
        self.assertEqual(state["sweep"][tag], old)
        self.assertTrue(state["sweep_pending"][tag]["restart"])

    def test_restart_marker_cleared_after_successful_retry(self):
        tag = finder.SWEEP_TAGS[0]
        old = finder.iso(finder.NOW - finder.timedelta(hours=2))
        stop = finder.iso(finder.NOW - finder.timedelta(hours=1))
        state = {"sweep": {t: finder.iso(finder.NOW) for t in finder.SWEEP_TAGS},
                 "sweep_pending": {tag: {"start": old, "stop": stop,
                                         "next_page": None, "pages": 0, "restart": True}}}
        state["sweep"][tag] = old
        finder.budget = FakeBudget(remaining=200)
        with patch.object(finder, "search_page",
                          return_value={"items": [], "pageInfo": {"totalResults": 0}}):
            finder.sweep(state, {}, {"videos": 0, "slices": 0, "overflow": 0})
        self.assertNotIn(tag, state["sweep_pending"])
        self.assertGreater(finder.parse_iso(state["sweep"][tag]), finder.parse_iso(old))

    def test_does_not_recheck_recent_failure(self):
        state = {"rejected_rechecked": {"UC_TEST": finder.iso(finder.NOW)}}
        with patch.object(finder, "fetch_channels") as fetch:
            finder.recheck_rejected({}, state, {"UC_TEST": finder.iso(finder.NOW)}, {"recovered": 0})
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

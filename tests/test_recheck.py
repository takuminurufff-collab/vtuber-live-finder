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

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
        self.assertNotIn("UC_HIDDEN", db)

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

    def test_does_not_recheck_recent_failure(self):
        state = {"rejected_rechecked": {"UC_TEST": finder.iso(finder.NOW)}}
        with patch.object(finder, "fetch_channels") as fetch:
            finder.recheck_rejected({}, state, {"UC_TEST": finder.iso(finder.NOW)}, {"recovered": 0})
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

"""Offline checks that the production workflow cannot overwrite newer main data."""
import pathlib
import unittest


WORKFLOW = (pathlib.Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "update.yml").read_text(encoding="utf-8")


class UpdateWorkflowSafetyTests(unittest.TestCase):
    def test_both_update_paths_require_fresh_main_before_push(self):
        self.assertEqual(WORKFLOW.count("git fetch origin main"), 2)
        self.assertEqual(
            WORKFLOW.count("git merge-base --is-ancestor origin/main HEAD"), 2)
        self.assertEqual(WORKFLOW.count("git push origin HEAD:main"), 2)
        self.assertNotIn("git pull --rebase -X theirs", WORKFLOW)

    def test_failed_collector_stages_only_quota_state(self):
        failed = WORKFLOW.split("      - name: API取得エラー時にも消費済み枠だけ保存", 1)[1]
        failed = failed.split("      - name: 新しく取得したデータを公開前に検証", 1)[0]
        self.assertIn("git add data/state.json", failed)
        self.assertNotIn("git add data/channels.json", failed)
        self.assertNotIn("git add site/data.json", failed)
        self.assertIn("steps.collect.outcome == 'failure'", failed)

    def test_deploy_happens_after_data_validation_and_safe_push(self):
        self.assertLess(WORKFLOW.index("      - name: 新しく取得したデータを公開前に検証"),
                        WORKFLOW.index("      - name: 蓄積データを保存"))
        self.assertLess(WORKFLOW.index("      - name: 蓄積データを保存"),
                        WORKFLOW.index("      - uses: actions/upload-pages-artifact"))


if __name__ == "__main__":
    unittest.main()

"""Offline checks that the production workflow cannot overwrite newer main data."""
import pathlib
import subprocess
import tempfile
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


class GitConcurrencyTests(unittest.TestCase):
    """Run the same fast-forward guard used by the deployment workflow."""

    def run_git(self, directory, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(directory), *args], check=check,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def initialize(self, root):
        remote = root / "origin.git"
        seed = root / "seed"
        worker = root / "worker"
        other = root / "other"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "clone", str(remote), str(seed)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for directory in (seed,):
            self.run_git(directory, "config", "user.name", "CI Test")
            self.run_git(directory, "config", "user.email", "ci@example.com")
        self.run_git(seed, "checkout", "-b", "main")
        (seed / "state.json").write_text("base", encoding="utf-8")
        self.run_git(seed, "add", "state.json")
        self.run_git(seed, "commit", "-m", "initial")
        self.run_git(seed, "push", "-u", "origin", "main")
        for dest in (worker, other):
            subprocess.run(["git", "clone", "--branch", "main", str(remote), str(dest)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.run_git(dest, "config", "user.name", "CI Test")
            self.run_git(dest, "config", "user.email", "ci@example.com")
        return remote, worker, other

    def safe_push(self, worker):
        self.run_git(worker, "fetch", "origin", "main")
        check = self.run_git(worker, "merge-base", "--is-ancestor", "origin/main",
                             "HEAD", check=False)
        if check.returncode:
            return False
        self.run_git(worker, "push", "origin", "HEAD:main")
        return True

    def test_push_with_unchanged_remote_succeeds(self):
        with tempfile.TemporaryDirectory() as temp:
            remote, worker, _ = self.initialize(pathlib.Path(temp))
            (worker / "state.json").write_text("worker", encoding="utf-8")
            self.run_git(worker, "commit", "-am", "worker update")
            self.assertTrue(self.safe_push(worker))
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "main:state.json"],
                check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "worker")

    def test_stale_push_does_not_revert_newer_remote_data(self):
        with tempfile.TemporaryDirectory() as temp:
            remote, worker, other = self.initialize(pathlib.Path(temp))
            (worker / "state.json").write_text("stale", encoding="utf-8")
            self.run_git(worker, "commit", "-am", "stale update")
            (other / "state.json").write_text("newer", encoding="utf-8")
            self.run_git(other, "commit", "-am", "newer update")
            self.run_git(other, "push", "origin", "HEAD:main")
            self.assertFalse(self.safe_push(worker))
            result = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "main:state.json"],
                check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "newer")


if __name__ == "__main__":
    unittest.main()

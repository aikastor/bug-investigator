import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from investigator import publication
from investigator.models import Target
from investigator.repository import Repository


class PublicationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.app = self.root / "app"
        self.remote = self.root / "remote.git"
        def git(*args):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        git("init", "-b", "main", str(self.app))
        (self.app / "ui.js").write_text("before\nbug\nafter\n")
        git("-C", str(self.app), "add", "ui.js")
        git("-C", str(self.app), "-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-m", "Base")
        git("clone", "--bare", str(self.app), str(self.remote))
        self.target = Target(container="test", container_source_root="/app", browser_source_root=".",
                             local_source_root=self.app, github={"repo": "test/app"},
                             baseline={"runner_image": "tests", "app_url": "http://app", "cases": {},
                                       "patch_sources": ["ui.js"]})
        self.fingerprint = Repository.load(self.app).fingerprint
        diff = "--- a/ui.js\n+++ b/ui.js\n@@ -1,3 +1,3 @@\n before\n-bug\n+fix\n after\n"
        suite = {"tests": {"test": {"outcome": "passed"}}}
        attempt = {"diff": diff, "candidate": {"observation": {"actual": "fixed"}}, "suite": suite}
        self.result = {"summary": "A bug", "baseline": {"scenario": "control", "runner_image": "tests",
                       "observation": {"actual": "bug"}}, "verification": {"outcome": "verified",
                       "baseline_suite": suite, "attempts": [attempt]}, "review": {"decision": "approved",
                       "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(), "decided_at": "2026-11-24"}}

    def test_recover_after_branch_push_without_a_second_branch_or_pr(self):
        real = publication.command
        prs = []
        bodies = []
        def command(args, *, cwd=None):
            if args[:3] == ["gh", "pr", "list"]:
                return json.dumps(prs)
            if args[:3] == ["gh", "pr", "create"]:
                bodies.append(Path(args[-1]).read_text())
                if len(bodies) == 1:
                    raise ValueError("Network interruption after push")
                prs.append({"url": "https://example.org/pull/1", "isCrossRepository": False})
                return prs[0]["url"]
            if "clone" in args:
                args = [str(self.remote) if arg == "https://github.com/test/app.git" else arg for arg in args]
            return real(args, cwd=cwd)
        with patch.object(publication, "command", side_effect=command):
            with self.assertRaisesRegex(ValueError, "interruption"):
                publication.publish(self.target, "abc", self.fingerprint, self.result)
            for _ in range(2):
                self.assertEqual(publication.publish(self.target, "abc", self.fingerprint, self.result), "https://example.org/pull/1")
            prs.clear()
            with self.assertRaisesRegex(ValueError, "differs"):
                publication.publish(self.target, "other", "wrong", self.result)
        self.assertEqual(len(bodies), 2)
        self.assertIn("fixed", bodies[-1])
        branches = real(["git", "--git-dir", str(self.remote), "branch", "--format=%(refname:short)"]).splitlines()
        branch, = [ref for ref in branches if ref != "main"]
        self.assertEqual(real(["git", "--git-dir", str(self.remote), "show", branch + ":ui.js"]), "before\nfix\nafter")
        self.assertEqual(real(["git", "--git-dir", str(self.remote), "show", "main:ui.js"]), "before\nbug\nafter")

    def test_unapproved_or_changed_diff_never_contacts_github(self):
        with patch.object(publication, "command") as remote:
            self.result["review"]["decision"] = "rejected"
            with self.assertRaises(ValueError):
                publication.publish(self.target, "abc", self.fingerprint, self.result)
            self.result["review"]["decision"] = "approved"
            self.result["verification"]["attempts"][-1]["diff"] += "other"
            with self.assertRaises(ValueError):
                publication.publish(self.target, "abc", self.fingerprint, self.result)
        remote.assert_not_called()

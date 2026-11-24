import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from investigator import jobs, local_patch, review
from investigator.models import Target
from investigator.repository import Repository


def sha(data):
    return hashlib.sha256(data).hexdigest()


class LocalPatchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.target = Target(container="app", container_source_root="/app", local_source_root=self.root,
                             browser_source_root=".", baseline={"patch_sources": ["a.js", "b.js"],
                             "runner_image": "runner", "app_url": "http://app", "cases": {}})
        self.original = b"before\nbug\nafter\n"
        self.fixed = self.original.replace(b"bug", b"fix")
        self.names = self.target.baseline.patch_sources
        for name in self.names:
            (self.root / name).write_bytes(self.original)
        self.fingerprint = Repository.load(self.root).fingerprint
        self.diff = "".join(f"--- a/{name}\n+++ b/{name}\n@@ -1,3 +1,3 @@\n before\n-bug\n+fix\n after\n"
                            for name in self.names)
        self.hashes = {n: {"before": sha(self.original), "after": sha(self.fixed)} for n in self.names}
        self.attempt = {"diff": self.diff, "file_hashes": self.hashes}
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        jobs.create_tables(self.db)
        self.db.execute("CREATE TABLE incidents(id TEXT PRIMARY KEY,record TEXT)")
        with self.db:
            self.db.execute("""INSERT INTO incidents VALUES ('id','{"target":"demo"}')""")
            self.db.execute("INSERT INTO investigations(incident_id,fingerprint,result) VALUES ('id',?,?)",
                            (self.fingerprint, json.dumps({"verification": {"outcome": "verified", "attempts": [self.attempt]}})))
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.addCleanup(conn.close)
        self.graph = review.build_graph(conn)
        self.token = review.advance(self.db, self.graph, "id")["token"]

    def run_worker(self):
        local_patch.apply_next(self.db, {"demo": self.target})
        result = self.db.execute("SELECT result FROM investigations").fetchone()[0]
        return json.loads(result).get("local_patch")

    def decide(self, choice):
        review.advance(self.db, self.graph, "id", choice, self.token)

    def test_approval_applies_only_missing_files_with_unrelated_edits(self):
        self.assertIsNone(self.run_worker())
        self.decide("approved")
        (self.root / "a.js").write_bytes(self.fixed)  # recovery after a partial write
        (self.root / "unrelated.js").write_text("work in progress")
        (self.root / "b.js").chmod(0o755)
        self.assertEqual(self.run_worker(), {"status": "applied", "files": self.names})
        for name in self.names:
            self.assertEqual((self.root / name).read_bytes(), self.fixed)
        self.assertEqual((self.root / "b.js").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.root / "unrelated.js").read_text(), "work in progress")
        self.decide("approved")
        self.assertEqual(self.run_worker()["status"], "applied")

    def test_rejection_does_not_write(self):
        self.decide("rejected")
        self.assertIsNone(self.run_worker())
        self.assertEqual((self.root / "a.js").read_bytes(), self.original)

    def test_conflict_preserves_all_files_and_can_be_retried(self):
        self.decide("approved")
        (self.root / "b.js").write_text("user edit")
        status = self.run_worker()
        self.assertEqual(status["status"], "failed")
        self.assertIn("b.js", status["error"])
        self.assertEqual((self.root / "a.js").read_bytes(), self.original)
        self.assertEqual((self.root / "b.js").read_text(), "user edit")
        (self.root / "b.js").write_bytes(self.original)
        self.decide("approved")
        self.assertEqual(self.run_worker()["status"], "applied")

    def test_symlink_is_never_followed(self):
        self.decide("approved")
        dest = self.root / "elsewhere"
        original = self.root / "b.js"
        original.rename(dest)
        original.symlink_to(dest)
        self.assertEqual(self.run_worker()["status"], "failed")
        self.assertEqual(dest.read_bytes(), self.original)
        self.assertTrue(original.is_symlink())

    def test_previously_approved_report_without_per_file_hashes(self):
        self.decide("approved")
        with self.db:
            self.db.execute("""UPDATE investigations SET result=json_remove(result,
                '$.verification.attempts[0].file_hashes', '$.local_patch')""")
        self.decide("approved")
        self.assertEqual(self.run_worker()["status"], "applied")
        self.assertEqual((self.root / "b.js").read_bytes(), self.fixed)

    def test_future_diagnosis_reads_the_running_source(self):
        (self.root / "a.js").write_bytes(self.fixed)
        def docker(*args):
            if args[0] == "cp":
                Path(args[-1]).write_bytes(self.original)
            return type("Result", (), {"stdout": "container-id"})()
        with patch("investigator.repository.docker", side_effect=docker):
            repo = Repository.load_target(self.target)
        self.assertEqual(repo.fingerprint, self.fingerprint)
        self.assertEqual(repo.files["a.js"][1], "bug")

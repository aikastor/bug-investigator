import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from investigator import jobs, review


class HumanReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.db = sqlite3.connect(self.path / "jobs.db")
        self.addCleanup(self.db.close)
        jobs.create_tables(self.db)
        self.result = {"verification": {"outcome": "verified", "attempts": [{"diff": "verified diff"}]}}
        with self.db:
            self.db.execute("INSERT INTO investigations(incident_id,result) VALUES ('id',?)", (json.dumps(self.result),))

    def graph(self):
        conn = sqlite3.connect(self.path / "checkpoints.db", check_same_thread=False)
        self.addCleanup(conn.close)
        return review.build_graph(conn)

    def test_resumes_once_after_restart_and_queues_only_one_pr(self):
        first = review.advance(self.db, self.graph(), "id")
        self.assertEqual(first["status"], "pending")
        graph = self.graph()
        self.assertEqual(first, review.advance(self.db, graph, "id"))
        config = {"configurable": {"thread_id": "id/" + first["diff_sha256"]}}
        self.assertTrue(graph.get_state(config).interrupts)
        def decide(value):
            return review.advance(self.db, graph, "id", value, first["token"], publishable=True)
        accepted = decide("approved")
        self.assertFalse(graph.get_state(config).interrupts)
        self.assertEqual(accepted, decide("approved"))
        with self.assertRaises(ValueError):
            decide("rejected")
        self.assertEqual(self.db.execute("SELECT status FROM publications").fetchall(), [("queued",)])

    def test_rejection_blocks_publication(self):
        graph = self.graph()
        token = review.advance(self.db, graph, "id")["token"]
        rejected = review.advance(self.db, graph, "id", "rejected", token, publishable=True)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.db.execute("SELECT count(*) FROM publications").fetchone()[0], 0)

    def test_stale_token_and_unverified_candidate(self):
        graph = self.graph()
        review.advance(self.db, graph, "id")
        with self.assertRaisesRegex(ValueError, "stale"):
            review.advance(self.db, graph, "id", "approved", "old", publishable=True)
        self.result["verification"]["outcome"] = "failed"
        with self.db:
            self.db.execute("UPDATE investigations SET result=?", (json.dumps(self.result),))
        with self.assertRaisesRegex(ValueError, "verified"):
            review.advance(self.db, graph, "id")

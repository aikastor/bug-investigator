import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx2
from openai import AsyncOpenAI

from investigator import jobs
from investigator.models import DockerBaseline, Incident, Target
from investigator.repository import Repository


class InvestigationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "ui.js").write_text("const fee = seats.length * 2;\n")
        (self.root / "server.js").write_text("if (fee !== 2) throw Error('Price mismatch');\n")
        (self.root / "secret.js").symlink_to(Path(__file__))
        self.target = Target(container="demo", container_source_root="/app",
                             browser_source_root=".", local_source_root=self.root)
        self.incident = Incident(id="abc", target="demo", kind="browser_error", frame=None,
                                 occurred_at=datetime.now(timezone.utc), message="Price mismatch",
                                 evidence="Error: Price mismatch\n  at requestBooking (app.js:12:3)")
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE incidents (id TEXT PRIMARY KEY, occurred TEXT, record TEXT)")
        jobs.create_tables(self.db)

    def test_dedup_and_reclaim(self):
        jobs.save_incidents(self.db, [self.incident, self.incident])
        self.assertEqual(jobs.claim(self.db, "first"), "abc")
        self.assertIsNone(jobs.claim(self.db, "second"))
        self.db.execute("UPDATE investigations SET lease_until=0")
        self.assertEqual(jobs.claim(self.db, "second"), "abc")
        self.assertIsNone(jobs.claim(self.db, "third"))

    def test_repository_is_scoped_and_stable(self):
        repo = Repository.load(self.root)
        (self.root / "ui.js").write_text("changed")
        self.assertIn("seats.length", repo.read("ui.js", 1, 1)[0]["text"])
        for path in ("secret.js", "../server.js", "/etc/passwd"):
            with self.assertRaises(ValueError):
                repo.read(path, 1, 1)

    async def test_worker_uses_real_graph_and_validates_citations(self):
        jobs.save_incidents(self.db, [self.incident])
        diagnosis = {"status": "diagnosed", "summary": "UI charges per seat, server expects one fee.",
                     "citations": [{"path": "ui.js", "line": 1}, {"path": "server.js", "line": 1}]}
        self.target.baseline = DockerBaseline(runner_image="test-image", app_url="http://app",
                                              cases={"fees": {"description": "Check fees", "command": ["test"]}})
        finish = {**diagnosis, "baseline_case": "fees"}
        calls = iter([
            ("search_repository", {"query": "fee"}),
            ("finish_investigation", finish),  # citations from a search alone are insufficient
            ("read_source", {"path": "server.js", "start_line": 1, "count": 3}),
            ("read_source", {"path": "ui.js", "start_line": 1, "count": 3}),
            ("finish_investigation", finish),
        ])
        requests = []

        def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            name, arguments = next(calls)
            call_id = f"call_{len(requests)}"
            return httpx2.Response(200, json={
                "id": "resp_" + call_id, "object": "response", "created_at": 1,
                "model": "gpt-6.1-sol", "status": "completed", "parallel_tool_calls": False,
                "tool_choice": "required", "tools": payload["tools"],
                "output": [{"type": "function_call", "call_id": call_id,
                            "name": name, "arguments": json.dumps(arguments)}],
            })

        client = AsyncOpenAI(api_key="test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
        baseline = {"scenario": "fees", "outcome": "reproduced"}
        with (patch.dict("os.environ", {"OPENAI_API_KEY": "test"}),
              patch.object(jobs, "AsyncOpenAI", return_value=client),
              patch("investigator.graph.run_baseline", return_value=baseline) as replay):
            worker = asyncio.create_task(jobs.work(self.db, {"demo": self.target}, self.root / ".env"))
            try:
                async with asyncio.timeout(8):
                    while True:
                        row = self.db.execute("SELECT status, tool_calls, result, error FROM investigations").fetchone()
                        if row[0] not in {"queued", "running"}:
                            break
                        await asyncio.sleep(0.01)
                self.assertEqual(row[0], "diagnosed", row[3])
                self.assertEqual(json.loads(row[2]), {**diagnosis, "baseline": baseline})
                replay.assert_called_once_with(self.incident, self.target, "fees")
                self.assertIn("error", json.loads(row[1])[1]["output"])
                self.assertIn("Price mismatch", requests[0]["input"][0]["content"])
                self.assertEqual(requests[1]["input"][-1]["type"], "function_call_output")
                jobs.save_incidents(self.db, [self.incident])
                self.assertIsNone(jobs.claim(self.db, "repeat"))
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx2
from fastapi import Request, Response
from openai import AsyncOpenAI

from investigator import api, jobs
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
        self.assertEqual(self.db.execute("SELECT execution_id, incident_id FROM investigation_executions ORDER BY rowid")
                         .fetchall(), [("first", "abc"), ("second", "abc")])

    async def json_report(self):
        request = Request({"type": "http", "headers": [], "query_string": b"format=json"})
        with (patch.object(api.app.state, "db", self.db, create=True),
              patch.object(api.app.state, "configured_targets", {"demo": self.target}, create=True)):
            return await api.investigation("abc", request, Response())

    async def test_historical_report_without_execution_still_opens(self):
        jobs.save_incidents(self.db, [self.incident])
        original = {"summary": "Earlier diagnosis", "citations": []}
        with self.db:
            self.db.execute("UPDATE investigations SET status='diagnosed', result=?", (json.dumps(original),))
        jobs.create_tables(self.db)
        report = await self.json_report()
        self.assertEqual(report["result"], original)
        self.assertIsNone(report["usage"])
        self.assertIsNone(report["trace"])

    async def test_failure_before_diagnosis_keeps_trace_and_received_usage(self):
        jobs.save_incidents(self.db, [self.incident])
        def respond(request):
            return httpx2.Response(200, json={
                "id": "resp_failed", "object": "response", "created_at": 1, "model": "returned-model",
                "status": "incomplete", "output": [], "usage": {"input_tokens": 17,
                "output_tokens": 3, "total_tokens": 20}})

        client = AsyncOpenAI(api_key="test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)))
        (self.root / ".env").write_text("OPENAI_API_KEY=test\nLANGSMITH_TRACING=true\nLANGSMITH_API_KEY=test")
        with (patch.dict("os.environ", {}, clear=True), patch.object(jobs, "AsyncOpenAI", return_value=client),
              patch("investigator.observability.Client") as exporter, self.assertLogs("investigator.jobs", "ERROR")):
            exporter.return_value.read_project.return_value.url = "https://smith.example/private-project"
            worker = asyncio.create_task(jobs.work(self.db, {"demo": self.target}, self.root / ".env"))
            try:
                async with asyncio.timeout(8):
                    while (await self.json_report())["status"] != "failed":
                        await asyncio.sleep(0.01)
                report = await self.json_report()
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        root, = exporter.return_value.request_with_retries.call_args.kwargs["request_kwargs"]["json"]["patch"]
        self.assertIsNone(report["result"])
        self.assertEqual(root["outputs"]["outcome"], "failed")
        self.assertEqual(root["extra"]["metadata"]["incident_id"], report["incident_id"])
        self.assertEqual(report["trace"]["execution_id"], root["id"])
        self.assertIn(root["id"], report["trace"]["url"])
        self.assertEqual(report["usage"]["total_tokens"], 20)
        self.assertEqual(root["outputs"]["total_tokens"], 20)

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
        with (patch.dict("os.environ", {"OPENAI_API_KEY": "test", "LANGSMITH_TRACING": "false"}),
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

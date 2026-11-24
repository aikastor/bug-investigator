import json
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from investigator.baseline import run_tests, test_results
from investigator.graph import investigate
from investigator.models import Incident, Target
from investigator.patches import apply_diff, compare, verify_candidate
from investigator.repository import Repository


DIFF = "--- a/ui.js\n+++ b/ui.js\n@@ -1,3 +1,3 @@\n before\n-bug\n+fix\n after\n"


def suite(*outcomes):
    return {"outcome": "failed", "command": ["suite"], "tests": {
        name: {"outcome": status, "detail": name + " assertion"}
        for name, status in zip(["selected", "control", "unrelated"], outcomes)}}


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "ui.js").write_text("before\nbug\nafter\n")
        self.repo = Repository.load(self.root)
        self.target = Target(container="demo", container_source_root="/app", local_source_root=self.root,
                             browser_source_root=".", baseline={"runner_image": "test", "app_url": "http://app",
                             "suite_command": ["suite"], "patch_sources": ["ui.js"],
                             "cases": {"example": {"description": "Check UI", "command": ["selected"]}}})
        self.baseline = {"outcome": "reproduced", "scenario": "example", "app_image": "original",
                         "runner_image": "pinned", "app_url": "http://app", "command": ["selected"],
                         "test_name": "selected", "detail": "actual != expected", "observation": {"inputs": "abc"}}
        self.before = suite("failed", "passed", "failed")

    def test_source_only_diff(self):
        for invalid in [DIFF.replace("ui.js", "../ui.js"), DIFF.replace("ui.js", "tests.js"),
                        DIFF.replace("+++ b/ui.js", "+++ b/other.js"), DIFF + "new file mode 100755\n"]:
            with self.assertRaises(ValueError):
                apply_diff(self.root, invalid, ["ui.js"])
        self.assertEqual(apply_diff(self.root, DIFF, ["ui.js"]), ["ui.js"])
        self.assertEqual((self.root / "ui.js").read_text(), "before\nfix\nafter\n")

    def test_drift_aborts_before_build_and_releases_resources(self):
        def docker(*args, **kwargs):
            if args[0] == "cp":
                Path(args[-1]).write_text("different baseline")
            return subprocess.CompletedProcess(args, 0, "", "")
        with (patch("investigator.patches.docker", side_effect=docker) as engine,
              patch("investigator.patches.cleanup") as clean):
            result = verify_candidate(self.target, self.repo, DIFF, self.baseline, self.before)
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn("differs", result["detail"])
        self.assertFalse(any(call.args[0] == "commit" for call in engine.call_args_list))
        self.assertEqual(clean.call_count, 2)

    def test_compare_inventory_and_new_failures(self):
        attempt = {"candidate": {**self.baseline, "outcome": "passed"}, "suite": suite("passed", "passed", "failed")}
        self.assertEqual(compare(attempt, self.baseline, self.before)["outcome"], "verified")
        attempt["suite"] = suite("passed", "failed", "failed")
        result = compare(attempt, self.baseline, self.before)
        self.assertEqual(result["new_failures"], ["control"])
        self.assertIn("control assertion", result["detail"])
        attempt["suite"]["tests"].pop("control")
        self.assertEqual(compare(attempt, self.baseline, self.before)["outcome"], "inconclusive")

    def test_incomplete_suite_and_runner_errors(self):
        line = 'TEST_RESULT {"name":"control","outcome":"failed","detail":"wrong value"}\n'
        for output, code in [(line, 1), (line + "TEST_PLAN 2", 1), (line + "TEST_PLAN 1", 125),
                             (line.replace('"failed"', '"inconclusive"') + "TEST_PLAN 1", 1)]:
            result = test_results(subprocess.CompletedProcess([], code, output, ""))
            self.assertEqual(result["outcome"], "inconclusive")
        result = test_results(subprocess.CompletedProcess([], 1, line + "TEST_PLAN 1", ""))
        self.assertEqual(result["outcome"], "failed")

    def test_timeout_releases_network_and_both_containers(self):
        with (patch("investigator.baseline.docker", side_effect=[Mock(), Mock(), subprocess.TimeoutExpired("run", 90)]),
              patch("investigator.baseline.cleanup") as clean):
            result = run_tests("app", "runner", ["suite"], "http://app")
        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual([call.args[1] for call in clean.call_args_list], ["container", "network"])
        self.assertTrue(clean.call_args_list[0].args[-1].endswith("-app"))
        self.assertTrue(clean.call_args_list[0].args[-2].endswith("-test"))

    async def run_graph(self, attempts, baseline_outcome="reproduced"):
        incident = Incident(id="abc", target="demo", kind="browser_error", frame=None,
                            occurred_at=datetime.now(timezone.utc), message="bug", evidence="bug")
        def response(name, args):
            call = {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": "c"}
            return SimpleNamespace(status="completed", output=[SimpleNamespace(**call, model_dump=lambda: call)])
        responses = [response("read_source", {"path": "ui.js", "start_line": 1, "count": 3}),
                     response("finish_investigation", {"status": "diagnosed", "summary": "bug",
                              "citations": [{"path": "ui.js", "line": 2}], "baseline_case": "example"}),
                     *(response("propose_patch", {"diff": attempt["diff"]}) for attempt in attempts)]
        client = Mock()
        client.responses.create = AsyncMock(side_effect=responses)
        with (patch("investigator.graph.run_baseline", return_value={**self.baseline, "outcome": baseline_outcome}),
              patch("investigator.graph.run_tests", return_value=self.before) as run_suite,
              patch("investigator.graph.verify_candidate", side_effect=attempts)):
            result = await investigate(incident, self.repo, client, Mock(), self.target)
        self.assertEqual(run_suite.call_count, int(baseline_outcome == "reproduced"))
        return result, client.responses.create.await_args_list

    async def test_two_revisions_use_latest_evidence_and_stop_after_three_candidates(self):
        first = {"outcome": "failed", "detail": "selected test failed", "diff": DIFF}
        second = {"outcome": "failed", "detail": "control regressed", "diff": DIFF.replace("+fix", "+revision")}
        for outcome in ("verified", "failed"):
            attempts = [first, second, {**second, "outcome": outcome}]
            result, requests = await self.run_graph(attempts)
            self.assertEqual(result["verification"]["outcome"], outcome)
            self.assertEqual(result["verification"]["attempts"], attempts)
            self.assertEqual(len(requests), 5)
            for call, previous in zip(requests[-2:], (first, second)):
                prompt = json.loads(call.kwargs["input"])
                self.assertEqual(prompt["failed_assertions"], previous["detail"])
                self.assertEqual(prompt["previous_diff"], previous["diff"])

    async def test_first_revision_can_finish_early(self):
        first = {"outcome": "failed", "detail": "wrong value", "diff": DIFF}
        for outcome in ("verified", "inconclusive"):
            attempts = [first, {**first, "outcome": outcome}]
            result, requests = await self.run_graph(attempts)
            self.assertEqual(result["verification"]["attempts"], attempts)
            self.assertEqual(len(requests), 4)

    async def test_no_revision_on_infrastructure_error(self):
        result, requests = await self.run_graph([{"outcome": "inconclusive", "detail": "runner unavailable", "diff": DIFF}])
        self.assertEqual(result["verification"]["outcome"], "inconclusive")
        self.assertEqual(result["baseline"], self.baseline)
        self.assertEqual(len(requests), 3)

    async def test_only_reproduced_baseline_proposes_fix(self):
        for outcome in ["passed", "inconclusive"]:
            result, requests = await self.run_graph([], outcome)
            self.assertNotIn("verification", result)
            self.assertEqual(len(requests), 2)

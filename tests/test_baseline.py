import json
import unittest
from subprocess import CompletedProcess
from types import SimpleNamespace

from investigator.baseline import outcome


class BaselineTests(unittest.TestCase):
    def classify(self, code=1, message="Price mismatch", assertion="ERR_ASSERTION"):
        evidence = {"scenario": "fees", "expected": "$26", "actual": "$28", "message": message}
        run = CompletedProcess([], code, "# BASELINE_RESULT " + json.dumps(evidence) + "\n" + assertion, "")
        return outcome(run, "fees", SimpleNamespace(message="Price mismatch"))["outcome"]

    def test_only_matching_failure_counts(self):
        self.assertEqual(self.classify(), "reproduced")
        self.assertEqual(self.classify(message="Seat already taken"), "inconclusive")
        self.assertEqual(self.classify(assertion="browser timed out"), "inconclusive")

    def test_passing_test_and_runner_failure(self):
        self.assertEqual(self.classify(code=0), "passed")
        run = CompletedProcess([], 125, "", "image unavailable")
        self.assertEqual(outcome(run, "fees", None)["outcome"], "inconclusive")

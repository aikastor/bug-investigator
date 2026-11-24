import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

from investigator.models import Target
from investigator.watcher import collect, find_frame


class WatcherTests(unittest.TestCase):
    timestamp = "2026-11-24T12:00:01.123456789Z"

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "public").mkdir()
        (root / "public/app.js").touch()
        self.target = Target(container="demo", local_source_root=root,
                             container_source_root="/app", browser_source_root="public")

    def collect_logs(self, state, logs):
        with patch("investigator.watcher.docker", side_effect=[state, logs]):
            return collect("demo", self.target)[1]

    def test_exited_container_without_logs(self):
        state = f"abc exited 7 {self.timestamp} {self.timestamp}"
        first, = self.collect_logs(state, "")
        self.assertEqual(first.id, self.collect_logs(state, "")[0].id)
        self.assertEqual(first.exit_code, 7)
        self.assertIsNone(first.frame)

    def test_normal_stop(self):
        state = f"abc exited 0 {self.timestamp} {self.timestamp}"
        self.assertEqual(self.collect_logs(state, ""), [])

    def test_browser_source_mapping(self):
        stack = "at showOwner (http://127.0.0.1:4173/app.js:30:8)"
        frame = find_frame(stack, self.target, browser=True)
        self.assertEqual((frame.path, frame.line), ("public/app.js", 30))

    def test_frames_outside_checkout(self):
        self.assertIsNone(find_frame("file:///app/../../README.md:3", self.target, browser=False))
        self.assertIsNone(find_frame("http://localhost/../../../README.md:1", self.target, browser=True))

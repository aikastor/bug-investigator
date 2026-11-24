import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import demo


class DemoSetupTests(unittest.TestCase):
    def test_first_and_repeat_launch_preserve_existing_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bug-investigator"
            root.mkdir()
            (root / "targets.toml").write_bytes((demo.ROOT / "targets.toml").read_bytes())
            checkout = root.parent / "faulty-app"
            python = root / ".venv/bin/python"

            with (patch.object(demo, "ROOT", root), patch.object(subprocess, "run") as run,
                  patch.object(subprocess, "check_output", side_effect=["", "runner-id\n"])):
                self.assertEqual(demo.prepare(), python)
                run.assert_any_call(["git", "clone", "https://github.com/aikastor/faulty-app.git",
                                     root / "../faulty-app"], check=True)
                run.assert_any_call([sys.executable, "-m", "venv", root / ".venv"], check=True)
                run.assert_any_call(["docker", "compose", "--profile", "tests", "build", "browser-tests"],
                                    cwd=root / "../faulty-app", check=True)

                checkout.mkdir()
                python.parent.mkdir(parents=True)
                python.touch()
                run.reset_mock()
                demo.prepare()
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(len(commands), 2)
                self.assertEqual(commands[0][:4], [python, "-m", "pip", "install"])
                self.assertEqual(commands[1], ["docker", "compose", "up", "-d", "--no-recreate", "buggy-react"])

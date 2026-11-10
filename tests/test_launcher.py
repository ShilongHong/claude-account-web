import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class LauncherTests(unittest.TestCase):
    def test_running_child_stays_on_concrete_profile_after_pointer_changes(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            one, two = tmp / "account a", tmp / "account b"
            one.mkdir(); two.mkdir()
            (one / "credential").write_text("test-a")
            (two / "credential").write_text("test-b")
            current = tmp / "current"
            current.symlink_to(one)
            binary = tmp / "fake-claude"
            binary.write_text('#!/usr/bin/env python3\nimport json,os,pathlib,sys\nprint("ready",flush=True)\nsys.stdin.readline()\np=pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"])\nprint(json.dumps({"directory":str(p),"credential":(p/"credential").read_text()}))\n')
            binary.chmod(0o700)
            process = subprocess.Popen([sys.executable, str(root / "claude-current")],
                                       env=dict(os.environ, CLAUDE_CONFIG_DIR=str(current), CLAUDE_ACCOUNT_WEB_REAL_CLAUDE=str(binary)),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                current.unlink(); current.symlink_to(two)
                output, _ = process.communicate("\n", timeout=5)
                result = json.loads(output)
                self.assertEqual(result["directory"], str(one))
                self.assertEqual(result["credential"], "test-a")
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()


if __name__ == "__main__":
    unittest.main()

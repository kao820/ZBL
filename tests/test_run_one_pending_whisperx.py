import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scripts.run_one_pending_whisperx as runner


class RunOnePendingWhisperxTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.cwd = os.getcwd()
        os.chdir(self.td.name)
        self.addCleanup(lambda: os.chdir(self.cwd))

    def write_status(self, status="pending", attempts=0):
        payload = {
            "videos": [
                {
                    "videoId": "abc123",
                    "url": "https://youtu.be/abc123",
                    "title": "Test",
                    "status": status,
                    "attempts": attempts,
                }
            ]
        }
        Path("playlist-video-status.json").write_text(json.dumps(payload), encoding="utf-8")

    def read_status(self):
        return json.loads(Path("playlist-video-status.json").read_text(encoding="utf-8"))

    @patch("scripts.run_one_pending_whisperx.subprocess.run")
    def test_success_when_returncode_zero_and_file_exists(self, run_mock):
        self.write_status()
        os.makedirs("playlist-video-transcripts", exist_ok=True)
        Path("playlist-video-transcripts/abc123.json").write_text("{}", encoding="utf-8")
        run_mock.return_value = SimpleNamespace(returncode=0)

        rc = runner.main()

        self.assertEqual(rc, 0)
        st = self.read_status()["videos"][0]
        self.assertEqual(st["status"], "indexed")
        self.assertEqual(st.get("lastError"), None)
        self.assertEqual(st["attempts"], 1)

    @patch("scripts.run_one_pending_whisperx.subprocess.run")
    def test_failure_when_returncode_zero_but_file_missing(self, run_mock):
        self.write_status(attempts=1)
        run_mock.return_value = SimpleNamespace(returncode=0)

        rc = runner.main()

        self.assertEqual(rc, 2)
        st = self.read_status()["videos"][0]
        self.assertEqual(st["status"], "retry")
        self.assertEqual(st["lastError"], "whisperx_rc_0")
        self.assertEqual(st["attempts"], 2)

    @patch("scripts.run_one_pending_whisperx.subprocess.run")
    def test_failure_becomes_missing_on_max_attempts(self, run_mock):
        self.write_status(attempts=4)
        run_mock.return_value = SimpleNamespace(returncode=1)

        rc = runner.main()

        self.assertEqual(rc, 1)
        st = self.read_status()["videos"][0]
        self.assertEqual(st["status"], "missing")
        self.assertEqual(st["lastError"], "whisperx_rc_1")
        self.assertEqual(st["attempts"], 5)


if __name__ == "__main__":
    unittest.main()

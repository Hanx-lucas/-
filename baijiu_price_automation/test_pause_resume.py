import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import ui_server as ui


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(ui.STATE)
        self.env = dict(ui.CONTROL_ENV)
        self.directory = tempfile.TemporaryDirectory()
        self.pause_file = Path(self.directory.name) / "pause"
        ui.CONTROL_ENV["RPA_PAUSE_FILE"] = str(self.pause_file)
        ui.STATE.update(running=True, job_id="test-job", progress=37, paused=False,
                        pause_requested=False, pausable=True, log="", result=None)

    def tearDown(self):
        self.pause_file.unlink(missing_ok=True)
        ui.STATE.clear(); ui.STATE.update(self.original)
        ui.CONTROL_ENV.clear(); ui.CONTROL_ENV.update(self.env)
        self.directory.cleanup()

    def test_stale_job_and_write_phase_cannot_pause(self):
        with self.assertRaises(ValueError): ui.request_pause("old-job")
        ui.STATE["pausable"] = False
        with self.assertRaises(ValueError): ui.request_pause("test-job")
        self.assertFalse(self.pause_file.exists())

    def test_same_child_resumes_with_cursor_and_timeout_excludes_pause(self):
        ui.request_pause("test-job")
        completed = []
        code = 'from run_control import wait_if_paused; print("cursor=1",flush=True); wait_if_paused(); print("cursor=2",flush=True)'
        worker = threading.Thread(target=lambda: completed.append(ui.stream_child(
            [sys.executable, "-u", "-c", code], ui.ROOT / "wechat-article-rpa", 1.5)))
        worker.start()
        deadline = time.monotonic() + 5
        while not ui.STATE["paused"] and time.monotonic() < deadline: time.sleep(.02)
        try:
            self.assertTrue(ui.STATE["paused"])
            self.assertEqual(ui.STATE["progress"], 37)
            time.sleep(1.7)
            self.assertTrue(worker.is_alive())
            self.assertNotIn("cursor=2", ui.STATE["log"])
        finally:
            ui.request_resume("test-job")
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(completed[0].returncode, 0)
        self.assertEqual(completed[0].stdout.count("cursor=1"), 1)
        self.assertEqual(completed[0].stdout.count("cursor=2"), 1)
        self.assertEqual(ui.STATE["progress"], 37)

    def test_pending_pause_cancel_and_writer_transition(self):
        ui.request_pause("test-job")
        ui.request_resume("test-job")
        ui.begin_write_phase()
        self.assertFalse(ui.STATE["pausable"])
        self.assertFalse(ui.STATE["paused"])
        self.assertFalse(self.pause_file.exists())

    def test_server_boundary_waits_then_keeps_progress(self):
        ui.request_pause("test-job")
        worker = threading.Thread(target=ui.begin_write_phase)
        worker.start()
        deadline = time.monotonic() + 2
        while not ui.STATE["paused"] and time.monotonic() < deadline: time.sleep(.01)
        self.assertTrue(ui.STATE["paused"])
        ui.request_resume("test-job"); worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(ui.STATE["progress"], 37)
        self.assertFalse(ui.STATE["pausable"])

if __name__ == "__main__": unittest.main()

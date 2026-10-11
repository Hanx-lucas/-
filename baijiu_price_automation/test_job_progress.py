import sys
import time
import unittest
from unittest.mock import patch
import ui_server as ui


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.original = dict(ui.STATE)
        ui.STATE.update(log='', progress=0, accounts_completed=0, accounts_total=0)

    def tearDown(self):
        ui.STATE.clear(); ui.STATE.update(self.original)

    def test_child_output_is_visible_before_process_finishes(self):
        observed = []
        consume = ui.consume_job_line
        def record(line):
            consume(line); observed.append((time.monotonic(), ui.STATE['message']))
        start = time.monotonic()
        command = [sys.executable, '-u', '-c',
                   'import time; print(\'RPA_PROGRESS {"percent": 30, "message": "working"}\',flush=True); time.sleep(.6); print("done",flush=True)']
        with patch.object(ui, 'consume_job_line', side_effect=record):
            completed = ui.stream_child(command, ui.BASE, 5)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(observed[0][1], 'working')
        self.assertLess(observed[0][0]-start, .5)
        self.assertGreater(time.monotonic()-observed[0][0], .5)

    def test_progress_uses_finished_accounts_and_does_not_move_back(self):
        ui.consume_job_line('{"event":"accounts_loaded","count":6}\n')
        ui.consume_job_line('{"event":"account_collection_finished","completed":2,"total":6,"account":"今日酒价"}\n')
        previous = ui.STATE['progress']
        ui.consume_job_line('RPA_PROGRESS {"percent":5,"message":"next"}\n')
        self.assertEqual(ui.STATE['progress'], previous)
        ui.consume_job_line('{"event":"article_open_attempt","account":"今日酒价","title":"报价","attempt":2}\n')
        self.assertIn('重试 1', ui.STATE['message'])

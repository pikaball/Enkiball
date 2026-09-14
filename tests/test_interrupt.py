from __future__ import annotations

import tempfile
import threading
import time
import unittest

from enkiball.llm import ChatEngine
from enkiball.sandbox import Sandbox, TurnInterrupted


class ExternalStopDuringToolTests(unittest.TestCase):
    def test_stop_callback_cancels_running_command(self) -> None:
        stop_requested = threading.Event()
        errors: list[BaseException] = []

        with tempfile.TemporaryDirectory() as workspace:
            engine = ChatEngine({}, sandbox=Sandbox(workspace, sandbox=False))
            turn = engine._begin_turn()
            watcher = engine._start_stop_watcher(turn, stop_requested.is_set)
            self.assertIsNotNone(watcher)

            def run_tool() -> None:
                try:
                    engine._dispatch_tool(
                        "run_command",
                        {"command": "sleep 30"},
                        turn,
                    )
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            worker = threading.Thread(target=run_tool)
            worker.start()
            time.sleep(0.2)
            stop_requested.set()
            worker.join(timeout=3)

            stopped, watcher_thread = watcher
            stopped.set()
            watcher_thread.join(timeout=1)
            engine._end_turn(turn)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], TurnInterrupted)
        self.assertTrue(turn.interrupted)


if __name__ == "__main__":
    unittest.main()

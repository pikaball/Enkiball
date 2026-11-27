from __future__ import annotations

import os
import pty
import termios
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from enkiball.cli import _StreamInputMonitor


class StreamInputMonitorTests(unittest.TestCase):
    @contextmanager
    def terminal(self):
        master, slave = pty.openpty()
        try:
            with os.fdopen(os.dup(slave), "r") as terminal:
                original = termios.tcgetattr(slave)
                with patch("enkiball.cli.sys.stdin", terminal):
                    with _StreamInputMonitor() as monitor:
                        yield master, slave, monitor
                self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            os.close(master)
            os.close(slave)

    def test_navigation_and_alt_keys_do_not_interrupt(self):
        sequences = (
            b"\x1b[5~", b"\x1b[6~",  # PgUp / PgDown.
            b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D",  # Arrows.
            b"\x1b[H", b"\x1b[F", b"\x1b[3~",  # Home / End / Delete.
            b"\x1b[5;2~", b"\x1b[6;5~", b"\x1b[5;3~",  # Modifiers.
            b"\x1bOA", b"\x1bOP", b"\x1bx",  # SS3 arrows / F1 / Alt-x.
        )
        for sequence in sequences:
            with self.subTest(sequence=sequence), self.terminal() as (master, _, monitor):
                os.write(master, sequence)
                self.assertFalse(monitor._stop_event.wait(0.2))
                self.assertFalse(monitor.exit_requested)

    def test_fragmented_page_keys_do_not_interrupt(self):
        for sequence in (b"\x1b[5~", b"\x1b[6~"):
            with self.subTest(sequence=sequence), self.terminal() as (master, _, monitor):
                for byte in sequence:
                    os.write(master, bytes([byte]))
                    time.sleep(0.02)
                self.assertFalse(monitor._stop_event.wait(0.2))

    def test_standalone_escape_interrupts_without_exiting(self):
        with self.terminal() as (master, _, monitor):
            os.write(master, b"\x1b")
            self.assertTrue(monitor._stop_event.wait(0.5))
            self.assertTrue(monitor.should_stop())
            self.assertTrue(monitor.interrupted)
            self.assertFalse(monitor.exit_requested)

    def test_escape_after_navigation_still_interrupts(self):
        with self.terminal() as (master, _, monitor):
            os.write(master, b"\x1b[5~\x1b[6~\x1b")
            self.assertTrue(monitor._stop_event.wait(0.5))
            self.assertFalse(monitor.exit_requested)

    def test_control_c_still_requests_exit(self):
        with self.terminal() as (master, slave, monitor):
            # A real terminal converts Ctrl-C into SIGINT. Disable that here
            # to exercise the monitor's byte-handling fallback without a signal.
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~termios.ISIG
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            os.write(master, b"\x1b[6~\x03")
            self.assertTrue(monitor._exit_event.wait(0.5))
            self.assertTrue(monitor.should_stop())

    def test_pause_discards_pending_escape_before_resume(self):
        with self.terminal() as (master, _, monitor):
            os.write(master, b"\x1b")
            time.sleep(0.02)
            monitor.pause()
            self.assertFalse(monitor.interrupted)
            monitor.resume()
            os.write(master, b"\x1b[6~")
            self.assertFalse(monitor._stop_event.wait(0.2))
            os.write(master, b"\x1b")
            self.assertTrue(monitor._stop_event.wait(0.5))


if __name__ == "__main__":
    unittest.main()

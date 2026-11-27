from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from enkiball.cli import _build_prompt_session


class CliInputTests(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait(), timeout=2)

    @asynccontextmanager
    async def prompt(self):
        history = InMemoryHistory([
            "AAAA oldest", "ZZZZ unrelated", "AAAA recent", "BBBB newest",
        ])
        with create_pipe_input() as pipe:
            with patch("enkiball.cli.PromptSession", side_effect=lambda **kwargs: PromptSession(
                input=pipe, output=DummyOutput(), **kwargs,
            )):
                session = _build_prompt_session(history, ["/help", "/history"])
            processed = asyncio.Event()
            session.key_bindings.add("c-]", eager=True)(lambda event: processed.set())

            async def send(keys):
                processed.clear()
                pipe.send_text(keys + "\x1d")
                await asyncio.wait_for(processed.wait(), timeout=2)

            task = asyncio.create_task(session.prompt_async("you> "))
            try:
                await self.wait_until(lambda: session.default_buffer._load_history_task is not None)
                await session.default_buffer._load_history_task
                yield session.default_buffer, send
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_arrows_keep_original_prefix_skip_unrelated_history_and_restore_draft(self):
        async with self.prompt() as (buffer, send):
            await send("AAAA")
            await self.wait_until(lambda: buffer.suggestion is not None)
            self.assertEqual(buffer.suggestion.text, " recent")
            for keys, expected in (
                ("\x1b[A", "AAAA recent"),
                ("\x1b[A", "AAAA oldest"),
                ("\x1b[A", "AAAA oldest"),
                ("\x1b[B", "AAAA recent"),
                ("\x1b[B", "AAAA"),
                ("\x1b[B", "AAAA"),
            ):
                await send(keys)
                self.assertEqual(buffer.text, expected)

    async def test_empty_prefix_browses_all_history_and_no_match_keeps_input(self):
        async with self.prompt() as (buffer, send):
            for keys, expected in (
                ("\x1b[A", "BBBB newest"),
                ("\x1b[A", "AAAA recent"),
                ("\x1b[B", "BBBB newest"),
                ("\x1b[B", ""),
                ("no match", "no match"),
                ("\x1b[A", "no match"),
                ("\x1b[B", "no match"),
            ):
                await send(keys)
                self.assertEqual(buffer.text, expected)

    async def test_editing_changes_prefix_and_right_arrow_accepts_grey_suggestion(self):
        async with self.prompt() as (buffer, send):
            await send("AAAA\x1b[A")
            self.assertEqual(buffer.text, "AAAA recent")
            await send("\x15ZZZZ\x1b[A")
            self.assertEqual(buffer.text, "ZZZZ unrelated")
            await send("\x1b[B")
            self.assertEqual(buffer.text, "ZZZZ")
            await send("\x15BBBB")
            await self.wait_until(lambda: buffer.suggestion is not None and buffer.suggestion.text == " newest")
            await send("\x1b[C")
            self.assertEqual(buffer.text, "BBBB newest")

    async def test_slash_completion_and_multiline_cursor_navigation_still_work(self):
        async with self.prompt() as (buffer, send):
            await send("/h")
            await self.wait_until(lambda: buffer.complete_state is not None)
            for keys, expected in (("\x1b[B", "/help"), ("\x1b[B", "/history"), ("\x1b[A", "/help")):
                await send(keys)
                self.assertEqual(buffer.text, expected)
            await send("\x15first\x0asecond")
            self.assertEqual(buffer.text, "first\nsecond")
            await send("\x1b[A")
            self.assertEqual(buffer.document.cursor_position_row, 0)
            await send("\x1b[B")
            self.assertEqual(buffer.document.cursor_position_row, 1)
            self.assertEqual(buffer.text, "first\nsecond")


if __name__ == "__main__":
    unittest.main()

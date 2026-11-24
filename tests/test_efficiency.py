from __future__ import annotations

import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from enkiball.llm import ChatEngine
from enkiball import Enkiball
from enkiball.mcp import MCPManager
from enkiball.message_codec import image_path_to_data_url


class ImageEncodingCacheTests(unittest.TestCase):
    def test_unchanged_image_is_read_once_and_changes_invalidate_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.png"
            image.write_bytes(b"first")
            original_read_bytes = Path.read_bytes
            reads = 0

            def counted_read_bytes(path: Path) -> bytes:
                nonlocal reads
                reads += 1
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", counted_read_bytes):
                first = image_path_to_data_url(str(image))
                second = image_path_to_data_url(str(image))
                image.write_bytes(b"second-version")
                changed = image_path_to_data_url(str(image))

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertEqual(reads, 2)


class MCPNormalizationTests(unittest.TestCase):
    def test_flattened_result_does_not_duplicate_raw_content_or_image_data(self) -> None:
        encoded_image = "base64-image-data"
        response = {
            "content": [
                {"type": "text", "text": "result text"},
                {"type": "image", "data": encoded_image, "mimeType": "image/png"},
            ],
            "structuredContent": {"count": 1},
            "isError": False,
        }

        with patch.object(
            ChatEngine, "_persist_mcp_image", return_value="/tmp/result.png"
        ):
            payload = json.loads(ChatEngine({}, state={})._format_mcp_result(response))

        serialized = json.dumps(payload)
        self.assertNotIn("raw", payload)
        self.assertNotIn(encoded_image, serialized)
        self.assertEqual(payload["text"], "result text")
        self.assertEqual(payload["images"], ["/tmp/result.png"])
        self.assertEqual(payload["structuredContent"], {"count": 1})

    def test_memory_agent_keeps_multiple_mcp_images_for_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            Path, "home", return_value=Path(directory)
        ), Enkiball(persist=False, skill_dirs=[]) as agent:
            engine = agent._engine
            content = [
                {"type": "text", "text": "two images"},
                {"type": "image", "data": "YQ==", "mimeType": "image/png"},
                {"type": "image", "data": "Yg==", "mimeType": "image/jpeg"},
            ]
            name = "mcp__local__images"
            with patch.object(agent.mcp, "call_tool", return_value={"content": content}):
                for async_mode in (False, True):
                    agent.notice_queue.append("notice")
                    if async_mode:
                        result = asyncio.run(engine._adispatch_tool(name, {}, Mock()))
                    else:
                        result = engine._dispatch_tool(name, {})
                    self.assertNotIn("base64", result)
                    self.assertIn("two images", result)
                    self.assertIn("notice", result)
                    engine._append_tool_result("call", result)
                    images = agent.messages[-1]["content"][1:]
                    self.assertEqual([part["image_url"]["url"] for part in images], [
                        "data:image/png;base64,YQ==", "data:image/jpeg;base64,Yg==",
                    ])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_restarting_stopped_client_closes_it_before_replacement(self) -> None:
        manager = MCPManager(
            {"mcp": {"servers": {"server": {"command": "server-command"}}}}
        )
        old_client = Mock(running=False)
        manager.clients["server"] = old_client
        replacement = Mock(running=True, tools=[], resources=[], prompts=[])

        with patch("enkiball.mcp.StdioMCPClient", return_value=replacement):
            ok, _ = manager.start("server")

        self.assertTrue(ok)
        old_client.close.assert_called_once_with()
        self.assertIs(manager.clients["server"], replacement)


if __name__ == "__main__":
    unittest.main()

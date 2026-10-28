from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from enkiball.llm import ChatEngine
from enkiball.llm_tools import ImageToolResult, TOOL_SCHEMAS, dispatch_tool
from enkiball.message_codec import prepare_messages_for_api
from enkiball.sandbox import Sandbox
from enkiball.tools import read_image
from test_stream_usage import Stream, chunk


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII="
)


class ReadImageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "picture.bin"
        self.path.write_bytes(PNG)
        self.sandbox = Sandbox(cwd=self.root)

    def test_dispatch_reads_relative_path_and_detects_mime_from_bytes(self):
        result = dispatch_tool("read_image", {"path": "picture.bin"}, self.sandbox)
        self.assertIsInstance(result, ImageToolResult)
        self.assertEqual(json.loads(result), {"ok": True, "path": str(self.path)})
        self.assertEqual(result.image_url, "data:image/png;base64," + base64.b64encode(PNG).decode())
        self.assertEqual([s["function"]["name"] for s in TOOL_SCHEMAS].count("read_image"), 1)

    def test_invalid_inputs_return_tool_errors(self):
        invalid = self.root / "invalid.png"
        invalid.write_text("not an image")
        for path in (None, "", "missing.png", ".", "invalid.png", "/outside/image.png"):
            with self.subTest(path=path):
                result = dispatch_tool("read_image", {"path": path}, self.sandbox)
                self.assertFalse(json.loads(result)["ok"])
                self.assertNotIsInstance(result, ImageToolResult)
        with patch("enkiball.tools.MAX_IMAGE_FILE_SIZE", 8):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                read_image(str(self.path))

    def test_read_only_permission_and_symlink_escape(self):
        child = self.root / "work"
        child.mkdir()
        sandbox = Sandbox(cwd=child)
        (child / "escape.png").symlink_to(self.path)
        self.assertFalse(json.loads(dispatch_tool("read_image", {"path": "escape.png"}, sandbox))["ok"])
        sandbox = Sandbox(cwd=child, ro_permissions=[self.root])
        self.assertIsInstance(dispatch_tool("read_image", {"path": "escape.png"}, sandbox), ImageToolResult)

    def test_sync_and_async_loops_send_images_after_all_tool_replies(self):
        for async_mode in (False, True):
            with self.subTest(async_mode=async_mode):
                self.path.write_bytes(PNG)
                engine = ChatEngine({}, state={}, model="test", sandbox=self.sandbox)
                engine.notice_queue.append("notice with image")
                requests = []
                calls = [{"index": i, "id": f"call-{i}", "type": "function",
                          "function": {"name": "read_image", "arguments": json.dumps({"path": "picture.bin"})}}
                         for i in range(2)]

                def create(**kwargs):
                    requests.append(kwargs)
                    if len(requests) == 1:
                        return Stream([chunk({"tool_calls": calls})])
                    self.path.unlink()
                    return Stream([chunk({"content": "image seen"})])

                client = Mock()
                client.chat.completions.create = create
                if async_mode:
                    async def acreate(**kwargs):
                        return create(**kwargs)

                    async def close():
                        pass

                    client.chat.completions.create = acreate
                    client.close = close
                with patch.object(engine, "_async_client" if async_mode else "_client", return_value=client):
                    reply = engine.astream_reply if async_mode else engine.stream_reply
                    result = reply("inspect picture.bin", [], lambda _: None)
                    if async_mode:
                        result = asyncio.run(result)
                self.assertEqual(result, "image seen")
                messages = requests[1]["messages"]
                self.assertEqual([m["role"] for m in messages[-4:]], ["assistant", "tool", "tool", "user"])
                self.assertIn("notice with image", messages[-3]["content"])
                images = [p for p in messages[-1]["content"] if p["type"] == "image_url"]
                self.assertEqual(len(images), 2)
                self.assertEqual(base64.b64decode(images[0]["image_url"]["url"].split(",")[1]), PNG)
                saved = json.loads(json.dumps(engine.export_messages()))
                self.assertEqual(prepare_messages_for_api(saved)[-2], messages[-1])
                self.assertEqual(sum(m["role"] == "user" for m in saved), 1)
                self.assertNotIn("base64", str(engine.capture()))

    def test_orphan_tool_group_does_not_inject_image(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "one"}, {"id": "two"}]},
            {"role": "tool", "tool_call_id": "one", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,unused"}},
            ]},
        ]
        self.assertEqual(prepare_messages_for_api(messages), [])

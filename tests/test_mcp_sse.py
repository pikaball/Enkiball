from __future__ import annotations

import unittest

from enkiball.mcp_client import SSEMCPClient


class SSEMCPClientEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SSEMCPClient("test", "https://example.com/events/stream")
        self.addCleanup(self.client.close)

    def endpoint_event(self, url: str) -> None:
        self.client._process_sse_event(["event: endpoint", f"data: {url}"])

    def test_accepts_relative_endpoint(self) -> None:
        self.endpoint_event("../messages")

        self.assertEqual(self.client.post_url, "https://example.com/messages")

    def test_accepts_absolute_endpoint_with_equivalent_default_port(self) -> None:
        self.endpoint_event("https://example.com:443/messages")

        self.assertEqual(self.client.post_url, "https://example.com:443/messages")

    def test_rejects_cross_origin_endpoints(self) -> None:
        endpoints = (
            "http://example.com/messages",
            "https://other.example/messages",
            "https://example.com:0/messages",
            "https://example.com:444/messages",
            "//other.example/messages",
        )

        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                self.client.post_url = None
                with self.assertRaisesRegex(ValueError, "same origin"):
                    self.endpoint_event(endpoint)
                self.assertIsNone(self.client.post_url)


if __name__ == "__main__":
    unittest.main()

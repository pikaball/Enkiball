"""Opt-in live integration test; invokes the configured Bifrost model.

Run from the repository after installing Enkiball, for example:
    python scripts/test_bifrost_protocols.py --model provider/model
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from enkiball import Enkiball


def exercise(protocol: str, args: argparse.Namespace) -> dict:
    with tempfile.TemporaryDirectory(prefix="enkiball-protocol-") as directory:
        root = Path(directory)
        base = args.base_url.rstrip("/") + ("/anthropic" if protocol == "anthropic" else "/v1")
        config = root / "config.json"
        config.write_text(json.dumps({"providers": {"bifrost": {
            "protocol": protocol, "api_key": os.environ.get("BIFROST_API_KEY", "local"),
            "base_url": base, "models": {args.model: {"max_tokens": 1024, "temperature": 1}},
        }}}))
        options = dict(config_path=config, state_path=root / "state.json", session_dir=root / "sessions",
                       cwd=root, skill_dirs=[], auto_start_mcp=False)
        calls = []
        cancelled = threading.Event()
        timer = threading.Timer(args.timeout, cancelled.set)
        timer.start()
        try:
            with patch("enkiball.llm.MAX_REQUEST_RETRIES", 0), Enkiball(**options) as agent:
                @agent.register_tool(name="protocol_probe", description="Return a verification marker for integer n.",
                                     parameters={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]})
                def probe(data):
                    calls.append(data)
                    return f"PROTOCOL_OK_{data['n']}"

                with patch.object(agent._engine, "_get_tools", return_value=agent._engine._custom_tool_schemas):
                    result = agent.chat("Call protocol_probe once with n=7, then reply with exactly its returned marker.", should_stop=cancelled.is_set)
                    assert calls == [{"n": 7}], calls
                    assert "PROTOCOL_OK_7" in result.content, result.content
                    assert not result.interrupted
                    saved = agent.save_session("protocol-check")
                    usage = agent.token_usage.to_dict()
                    assert usage["total_tokens"] > 0

                with Enkiball(**options) as restored, patch.object(restored._engine, "_get_tools", return_value=[]):
                    restored.load_session(saved.session_id)
                    assert restored.token_usage.to_dict() == usage
                    reply = asyncio.run(restored.achat("Repeat the exact verification marker returned earlier, and nothing else.", should_stop=cancelled.is_set))
                    assert "PROTOCOL_OK_7" in reply.content, reply.content
                    assert not reply.interrupted
                    summary = asyncio.run(restored.acompact(prompt="Summarize the verification result in one short sentence, including the exact marker.", should_stop=cancelled.is_set))
                    assert "PROTOCOL_OK_7" in summary, summary
                    return {"protocol": protocol, "tool_calls": len(calls), "restored": True,
                            "async_reply": reply.content, "compact": True, "usage": restored.token_usage.to_dict()}
        finally:
            timer.cancel()
            timer.join()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4011")
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--protocol", choices=("chat-completions", "responses", "anthropic"))
    args = parser.parse_args()
    for protocol in ([args.protocol] if args.protocol else ["chat-completions", "responses", "anthropic"]):
        print(json.dumps(exercise(protocol, args), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

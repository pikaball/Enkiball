from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("config/agent.json")
DEFAULT_ENV_PATH = Path(".env")
SECRET_LLM_FIELDS = {"api_key", "api_base", "api_key_env"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
            continue
        merged[key] = value
    return merged


def _parse_env_line(raw: str) -> tuple[str, str] | None:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    if "=" not in line:
        return None

    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_line(raw)
        if not parsed:
            continue
        key, value = parsed
        os.environ[key] = value


def _sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    out = dict(config)
    llm = dict(out.get("llm", {}))
    for key in SECRET_LLM_FIELDS:
        llm.pop(key, None)
    out["llm"] = llm
    return out


def ensure_config(path: Path = DEFAULT_CONFIG_PATH) -> Path:
    if not path.exists():
        # path.parent.mkdir(parents=True, exist_ok=True)
        # save_config(DEFAULT_CONFIG, path)
        raise FileNotFoundError(f"Config file not found: {path}")
    return path


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    load_dotenv(DEFAULT_ENV_PATH)
    ensure_config(path)
    with path.open("r", encoding="utf-8") as f:
        loaded = json.load(f)
    # merged = _deep_merge(DEFAULT_CONFIG, loaded)
    return _sanitize_config(loaded)


def save_config(config: dict[str, Any], path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sanitized = _sanitize_config(config)
    with path.open("w", encoding="utf-8") as f:
        json.dump(sanitized, f, ensure_ascii=False, indent=2)
        f.write("\n")


def list_mcp_servers(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return config.get("mcp", {}).get("servers", {})


def set_mcp_enabled(config: dict[str, Any], name: str, enabled: bool) -> bool:
    servers = list_mcp_servers(config)
    if name not in servers:
        return False
    servers[name]["enabled"] = enabled
    return True

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .persistence import atomic_write_json

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "enkiball" / "enki.json"
DEFAULT_STATE_PATH = Path.home() / ".config" / "enkiball" / "state.json"

log = logging.getLogger(__name__)


# -- State file (active_provider / active_model) -----------------------------


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Load the runtime state file.  Returns empty dict if missing."""
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.debug("load_state failed: %s", exc)
        return {}


def save_state(state: dict[str, Any], path: Path = DEFAULT_STATE_PATH) -> None:
    """Persist the runtime state (active_provider, active_model, etc.)."""
    atomic_write_json(path, state)


# -- Load / save -------------------------------------------------------------


def ensure_config(path: Path = DEFAULT_CONFIG_PATH) -> Path:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            f"Create it with at least one provider entry.  Example:\n"
            f"  mkdir -p {path.parent}\n"
            f"  cat > {path} << 'EOF'\n"
            f"{{\n"
            f'  "providers": {{\n'
            f'    "my-provider": {{\n'
            f'      "api_key": "sk-xxx",\n'
            f'      "base_url": "https://api.openai.com/v1",\n'
            f'      "models": {{\n'
            f'        "gpt-4o": {{ "temperature": 0.2, "max_tokens": 4096 }}\n'
            f"      }}\n"
            f"    }}\n"
            f"  }}\n"
            f"}}\n"
            f"EOF"
        )
    return path


def _migrate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy config formats to current format."""
    if "providers" not in raw:
        # Legacy single-provider format
        old_llm = raw.get("llm", {})
        provider_name = old_llm.get("provider", "").strip() or "default"
        model = old_llm.get("model", "")

        provider_entry: dict[str, Any] = {}
        if old_llm.get("api_key"):
            provider_entry["api_key"] = old_llm["api_key"]
        if old_llm.get("api_base"):
            provider_entry["base_url"] = old_llm["api_base"]
        if model:
            model_params: dict[str, Any] = {}
            if "temperature" in old_llm:
                model_params["temperature"] = old_llm["temperature"]
            if "max_tokens" in old_llm:
                model_params["max_tokens"] = old_llm["max_tokens"]
            provider_entry["models"] = {model: model_params}

        new: dict[str, Any] = {
            key: value for key, value in raw.items() if key != "llm"
        }
        new.update(
            {
                "providers": {provider_name: provider_entry},
                # These fields are moved to state.json by load_config.
                "active_provider": provider_name,
                "active_model": model,
            }
        )
        return new

    # Migrate models from list to dict if needed
    for _name, prov in raw.get("providers", {}).items():
        prov.pop("api_key_env", None)
        prov.pop("base_url_env", None)
        models = prov.get("models")
        if isinstance(models, list):
            # Convert list to dict with empty params
            prov["models"] = {m: {} for m in models}

    # Move top-level llm params into per-model defaults if still present
    old_llm = raw.pop("llm", None)
    if isinstance(old_llm, dict):
        for _name, prov in raw.get("providers", {}).items():
            for _model, params in prov.get("models", {}).items():
                for k in ("temperature", "max_tokens"):
                    if k not in params and k in old_llm:
                        params[k] = old_llm[k]

    return raw


def load_config(
    path: Path = DEFAULT_CONFIG_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_config(path)
    with path.open("r", encoding="utf-8") as f:
        loaded = json.load(f)
    config = _migrate_config(loaded)

    # Migrate active_provider / active_model from config to state file
    migrated = False
    state = load_state(state_path) if state is None else state
    for key in ("active_provider", "active_model"):
        if key in config:
            if key not in state:
                state[key] = config[key]
            del config[key]
            migrated = True
    if migrated:
        save_state(state, state_path)
        save_config(config, path)

    return config


def save_config(config: dict[str, Any], path: Path = DEFAULT_CONFIG_PATH) -> None:
    atomic_write_json(path, config)


# -- Provider helpers --------------------------------------------------------


def get_providers(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return config.get("providers", {})


def get_active_provider(
    config: dict[str, Any], state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None,
) -> str:
    state = load_state(state_path) if state is None else state
    name = str(state.get("active_provider", ""))
    if name and name in config.get("providers", {}):
        return name
    # Fallback: first provider
    providers = config.get("providers", {})
    return next(iter(providers), "") if providers else ""


def get_active_model(
    config: dict[str, Any], state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None,
) -> str:
    state = load_state(state_path) if state is None else state
    return str(state.get("active_model", ""))


def set_active_provider(
    config: dict[str, Any],
    name: str,
    state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None,
) -> bool:
    if name not in config.get("providers", {}):
        return False
    state = load_state(state_path) if state is None else state
    state["active_provider"] = name
    prov = config["providers"][name]
    models = prov.get("models", {})
    current_model = state.get("active_model", "")
    if models and current_model not in models:
        state["active_model"] = next(iter(models))
    save_state(state, state_path)
    return True


def set_active_model(
    config: dict[str, Any],
    model: str,
    state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None,
) -> None:
    state = load_state(state_path) if state is None else state
    state["active_model"] = model
    save_state(state, state_path)


def get_model_params(
    config: dict[str, Any],
    model: str | None = None,
    state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return per-model parameters (temperature, max_tokens, etc.) for the given model.

    Falls back to empty dict for models not defined in config (e.g. remote-only).
    """
    model = model or get_active_model(config, state_path, state=state)
    provider_name = get_active_provider(config, state_path, state=state)
    prov = config.get("providers", {}).get(provider_name, {})
    models = prov.get("models", {})
    if isinstance(models, dict):
        return dict(models.get(model, {}))
    return {}


def resolve_provider_credentials(
    config: dict[str, Any], state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return (api_key, base_url) for the active provider.

    Reads directly from the provider entry in config — no env vars.
    """
    provider_name = get_active_provider(config, state_path, state=state)
    prov = config.get("providers", {}).get(provider_name, {})
    api_key = str(prov.get("api_key", "")).strip()
    base_url = str(prov.get("base_url", "")).strip()
    return api_key, base_url


def _resolve_credentials_for(
    config: dict[str, Any], provider_name: str
) -> tuple[str, str]:
    """Return (api_key, base_url) for a specific provider by name."""
    prov = config.get("providers", {}).get(provider_name, {})
    api_key = str(prov.get("api_key", "")).strip()
    base_url = str(prov.get("base_url", "")).strip()
    return api_key, base_url


# -- System prompt -----------------------------------------------------------


def get_system_prompt(config: dict[str, Any]) -> str | None:
    """Return the custom system prompt from config, or None if not set."""
    return config.get("system_prompt") or None


# -- Remote model fetching ---------------------------------------------------


def fetch_remote_models(api_key: str, base_url: str, timeout: float = 5.0) -> list[str]:
    """Query ``/v1/models`` (or ``/models``) and return a sorted list of model IDs.

    Returns an empty list on any network or parse error (never raises).
    """
    if not api_key:
        return []

    url = base_url.rstrip("/") if base_url else "https://api.openai.com/v1"
    models_url = f"{url}/models"

    try:
        resp = httpx.get(
            models_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", [])
        ids = sorted({str(m.get("id", "")) for m in data if m.get("id")})
        return ids
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_remote_models failed for %s: %s", models_url, exc)
        return []


def get_provider_models(
    config: dict[str, Any],
    provider_name: str | None = None,
    *,
    remote: bool = True,
    timeout: float = 5.0,
    state_path: Path = DEFAULT_STATE_PATH,
    state: dict[str, Any] | None = None,
) -> list[str]:
    """Return merged model list for a provider: config-defined + remote.

    Config-defined models appear first, followed by remote-only models sorted.
    """
    name = provider_name or get_active_provider(config, state_path, state=state)
    prov = config.get("providers", {}).get(name, {})
    models = prov.get("models", {})
    config_models: list[str] = (
        list(models.keys()) if isinstance(models, dict) else list(models)
    )

    if not remote:
        return config_models

    api_key, base_url = _resolve_credentials_for(config, name)
    remote_models = fetch_remote_models(api_key, base_url, timeout=timeout)

    seen = set(config_models)
    merged = list(config_models)
    for m in remote_models:
        if m not in seen:
            merged.append(m)
            seen.add(m)
    return merged


# -- MCP helpers -------------------------------------------------------------


def list_mcp_servers(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return config.get("mcp", {}).get("servers", {})


def set_mcp_enabled(config: dict[str, Any], name: str, enabled: bool) -> bool:
    servers = list_mcp_servers(config)
    if name not in servers:
        return False
    servers[name]["enabled"] = enabled
    return True

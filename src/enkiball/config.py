from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .persistence import atomic_write_json

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "enkiball" / "enki.json"
DEFAULT_STATE_PATH = Path.home() / ".config" / "enkiball" / "state.json"
PROTOCOLS = ("chat-completions", "responses", "anthropic")
DEFAULT_PROTOCOL = "responses"

DEFAULT_CONFIG: dict[str, Any] = {
    "system_prompt": "You are Enkiball, a practical assistant for long-context tasks.",
    "auto_compact": {"enabled": False, "threshold_ratio": 0.7},
    "providers": {
        "openai": {
            "protocol": DEFAULT_PROTOCOL,
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "models": {"gpt-4o": {"temperature": 0.2, "max_tokens": 4096}},
        }
    },
    "active_provider": "openai",
    "active_model": "gpt-4o",
    "mcp": {"servers": {}},
}

log = logging.getLogger(__name__)


# -- State file (active_provider / active_model) -----------------------------


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, Any]:
    """Load the runtime state file.  Returns empty dict if missing."""
    path = Path(path).expanduser()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.debug("load_state failed: %s", exc)
        return {}


def save_state(state: dict[str, Any], path: Path | str = DEFAULT_STATE_PATH) -> None:
    """Persist the runtime state (active_provider, active_model, etc.)."""
    atomic_write_json(Path(path).expanduser(), state)


# -- Load / save -------------------------------------------------------------


def ensure_config(path: Path = DEFAULT_CONFIG_PATH) -> Path:
    path = Path(path).expanduser()
    if not path.exists():
        atomic_write_json(path, DEFAULT_CONFIG)
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
        if "protocol" in old_llm:
            provider_entry["protocol"] = old_llm["protocol"]
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


def prepare_config(
    raw: dict[str, Any],
    path: Path | None = None,
    state_path: Path = DEFAULT_STATE_PATH,
    *,
    state: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Migrate and validate an in-memory config mapping.

    ``active_provider`` / ``active_model`` are moved into *state*. Nothing is
    written to disk unless *persist* is true and a migration was required.
    *path* is only written when given (in-memory configs pass ``None``).
    """
    config = _migrate_config(copy.deepcopy(raw))
    for name, provider in config.get("providers", {}).items():
        validate_protocol(provider.get("protocol", DEFAULT_PROTOCOL), name)

    migrated = False
    state = load_state(state_path) if state is None else state
    for key in ("active_provider", "active_model"):
        if key in config:
            if key not in state:
                state[key] = config[key]
            del config[key]
            migrated = True
    if migrated and persist:
        save_state(state, state_path)
        if path is not None:
            save_config(config, path)

    return config


def load_config(
    path: Path = DEFAULT_CONFIG_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    *,
    state: dict[str, Any] | None = None,
    persist: bool = True,
    create_if_missing: bool = True,
) -> dict[str, Any]:
    path = Path(path).expanduser()
    state_path = Path(state_path).expanduser()
    if not path.exists():
        if not create_if_missing:
            raise FileNotFoundError(f"config file not found: {path}")
        if not persist:
            return prepare_config(
                DEFAULT_CONFIG, path, state_path, state=state, persist=False
            )
        ensure_config(path)
    with path.open("r", encoding="utf-8") as f:
        loaded = json.load(f)
    return prepare_config(
        loaded, path, state_path, state=state, persist=persist
    )


def save_config(config: dict[str, Any], path: Path | str = DEFAULT_CONFIG_PATH) -> None:
    atomic_write_json(Path(path).expanduser(), config)


# -- Provider helpers --------------------------------------------------------


def get_providers(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return config.get("providers", {})


def validate_protocol(value: Any, provider: str = "") -> str:
    if value not in PROTOCOLS:
        raise ValueError(f"invalid protocol for provider {provider!r}: {value!r}; expected one of {PROTOCOLS}")
    return value


def get_provider_protocol(
    config: dict[str, Any], state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None,
) -> str:
    name = get_active_provider(config, state_path, state=state)
    provider = config.get("providers", {}).get(name, {})
    return validate_protocol(provider.get("protocol", DEFAULT_PROTOCOL), name)


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
    model = state.get("active_model")
    if isinstance(model, str) and model:
        return model
    provider = get_active_provider(config, state_path, state=state)
    models = config.get("providers", {}).get(provider, {}).get("models", {})
    return next(iter(models), "")


def set_active_provider(
    config: dict[str, Any],
    name: str,
    state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None, persist: bool = True,
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
    if persist:
        save_state(state, state_path)
    return True


def set_active_model(
    config: dict[str, Any],
    model: str,
    state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None, persist: bool = True,
) -> None:
    state = load_state(state_path) if state is None else state
    state["active_model"] = model
    if persist:
        save_state(state, state_path)


def get_show_thinking(
    state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None,
) -> bool:
    state = load_state(state_path) if state is None else state
    return bool(state.get("show_thinking", False))


def set_show_thinking(
    enabled: bool,
    state_path: Path = DEFAULT_STATE_PATH,
    *, state: dict[str, Any] | None = None, persist: bool = True,
) -> None:
    state = load_state(state_path) if state is None else state
    state["show_thinking"] = bool(enabled)
    if persist:
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


def fetch_remote_models(
    api_key: str, base_url: str, timeout: float = 5.0, *, protocol: str = DEFAULT_PROTOCOL,
) -> list[str]:
    """Query ``/v1/models`` (or ``/models``) and return a sorted list of model IDs.

    An empty *api_key* is allowed for endpoints that do not require auth (for
    example local Ollama / LM Studio); no auth header is sent in that case.
    Returns an empty list on any network or parse error (never raises).
    """
    from .protocols import api_base_url

    url = api_base_url(protocol, base_url)
    models_url = f"{url}/models"
    headers: dict[str, str] = {}
    if api_key:
        headers = ({"x-api-key": api_key, "anthropic-version": "2023-06-01"}
                   if protocol == "anthropic" else {"Authorization": f"Bearer {api_key}"})

    try:
        resp = httpx.get(
            models_url,
            headers=headers,
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
    """Return the merged model list for a provider.

    The endpoint (``/models``) is the source of truth for what is available;
    manually configured ``models`` entries are merged on top so they can carry
    explicit parameters (``max_tokens`` etc.) or name models the endpoint does
    not advertise. Remote models come first, configured-only models are
    appended; on a name collision the configured entry wins.
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
    remote_models = fetch_remote_models(api_key, base_url, timeout=timeout,
                                       protocol=validate_protocol(prov.get("protocol", DEFAULT_PROTOCOL), name))

    merged = list(remote_models)
    seen = set(merged)
    for model in config_models:
        if model not in seen:
            merged.append(model)
            seen.add(model)
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

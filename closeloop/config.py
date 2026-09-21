"""Load config.json (the source of truth for API keys and settings). Live only."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

_PLACEHOLDER_MARKERS = ("PUT-YOUR", "YOUR-KEY")


def load():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # config.json is the source of truth; env vars are a fallback only when the
    # file has no real key.
    if _is_missing(cfg["jev"].get("api_key")) and os.environ.get("TYPESAFE_API_KEY"):
        cfg["jev"]["api_key"] = os.environ["TYPESAFE_API_KEY"]
    if _is_missing(cfg["openai"].get("api_key")) and os.environ.get("OPENAI_API_KEY"):
        cfg["openai"]["api_key"] = os.environ["OPENAI_API_KEY"]

    # No mock mode: a missing key is a hard startup error (H2/H3).
    for name, env in (("jev", "TYPESAFE_API_KEY"), ("openai", "OPENAI_API_KEY")):
        if _is_missing(cfg[name].get("api_key")):
            raise RuntimeError(
                f"Missing {name} API key. Set it in config.json (or the {env} env var). "
                "CloseLoop is live-only; there is no mock mode.")
    return cfg


def _is_missing(key):
    return not key or any(key.startswith(m) for m in _PLACEHOLDER_MARKERS)


def is_placeholder(key):
    """True when a key is empty or still the config template placeholder."""
    return _is_missing(key)


def load_raw():
    """Read config.json verbatim (no env merge, no validation) — for the settings UI."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_raw(cfg):
    """Write config.json (settings UI). Caller is responsible for validity."""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

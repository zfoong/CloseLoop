"""Load config.json and decide whether each backend runs live or mocked."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

_PLACEHOLDER_MARKERS = ("PUT-YOUR", "YOUR-KEY", "")


def load():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # config.json is the source of truth; environment variables are only a
    # fallback when the file has no real key.
    if _is_missing(cfg["jev"].get("api_key")) and os.environ.get("TYPESAFE_API_KEY"):
        cfg["jev"]["api_key"] = os.environ["TYPESAFE_API_KEY"]
    if _is_missing(cfg["openai"].get("api_key")) and os.environ.get("OPENAI_API_KEY"):
        cfg["openai"]["api_key"] = os.environ["OPENAI_API_KEY"]

    cfg["jev"]["mock"] = _is_missing(cfg["jev"].get("api_key"))
    cfg["openai"]["mock"] = _is_missing(cfg["openai"].get("api_key"))
    return cfg


def _is_missing(key):
    if not key:
        return True
    return any(key.startswith(m) for m in _PLACEHOLDER_MARKERS if m)

"""Content-addressed asset store (REQUIREMENT.md §15, scoreboard G1).

Arbitrary bytes (uploaded inputs, produced artifacts) live here, addressed by
sha256. The tree never sees bytes — only an asset *reference*:

    {"asset_id", "sha256", "name", "mime", "size", "meta": {...}}

Bytes are stored once per hash; a sidecar <asset_id>.json holds the reference
plus provenance (§15.4).
"""
import hashlib
import json
import os

from . import logsetup
from .config import ROOT

log = logsetup.get("assets")

ASSETS_DIR = os.path.join(ROOT, "data", "assets")


def put(data, name="asset", mime="application/octet-stream", meta=None):
    """Store bytes (or str); return a JSON-safe asset reference."""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    sha = hashlib.sha256(data).hexdigest()
    asset_id = sha[:16]
    blob = os.path.join(ASSETS_DIR, sha)
    if not os.path.exists(blob):
        with open(blob, "wb") as f:
            f.write(data)
    ref = {"asset_id": asset_id, "sha256": sha, "name": name, "mime": mime,
           "size": len(data), "meta": meta or {}}
    with open(os.path.join(ASSETS_DIR, asset_id + ".json"), "w", encoding="utf-8") as f:
        json.dump(ref, f, indent=2)
    log.info("asset stored: id=%s name=%s mime=%s size=%d", asset_id, name, mime, len(data))
    return ref


def get_ref(asset_id):
    path = os.path.join(ASSETS_DIR, str(asset_id) + ".json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_bytes(asset_id):
    ref = get_ref(asset_id)
    if ref is None:
        raise FileNotFoundError(f"unknown asset '{asset_id}'")
    with open(os.path.join(ASSETS_DIR, ref["sha256"]), "rb") as f:
        return f.read()

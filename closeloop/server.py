"""Zero-dependency HTTP server: serves the UI and the multi-port JSON API.

  GET  /                       -> ui/index.html
  GET  /api/capabilities       -> input/output types, roles, tools (for the wizard)
  GET  /api/tasks              -> list of tasks
  GET  /api/state?task=<id>    -> contract, tree, versions, runs, metrics
  GET  /api/tree?task&version  -> a specific tree version
  GET  /api/download?asset=<id>-> download an asset's bytes
  POST /api/upload             -> store raw bytes -> asset ref (X-Filename header)
  POST /api/tasks              -> create a task {name, description, inputs[], outputs[], examples}
  POST /api/run                -> run one input bundle. Either JSON {task, bundle}, OR
                                  multipart/form-data (field `task` + one field per input
                                  port, files inline — one call, no separate /api/upload)
  POST /api/generate           -> {task, count}   synthesize N input bundles
  POST /api/stream             -> {task, count}   generate + run N bundles
  POST /api/shape              -> {task}          force a shaping pass
  POST /api/compile            -> {task}          compile the current tree against the contract
  POST /api/rollback           -> {task, version, pin?}  set active version / pin
  POST /api/pin-fixture        -> {task, run_ts}  pin a run as a regression fixture
  POST /api/update-task        -> {task, name?, description?}  edit task metadata
  POST /api/delete-task        -> {task}          delete a task (and its versions/runs)
  GET  /api/config             -> models, log level, key status (redacted)
  POST /api/config             -> update models / keys / log level, then reload
"""
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import adapters, assets, config as config_mod, logsetup, schema, store, tools, tree as tree_mod
from .config import ROOT
from .engine import Engine, ARCHITECT_SYSTEM, SHAPER_SYSTEM, GENERATOR_SYSTEM
from .jev import JevClient
from .llm import LLMClient

UI_PATH = os.path.join(ROOT, "ui", "index.html")
_lock = threading.Lock()
log = logsetup.get("server")


def build_engine():
    cfg = config_mod.load()
    logsetup.setup_logging(cfg)
    tools.init(cfg)
    log.info("starting CloseLoop | models: jev=%s openai=%s", cfg["jev"].get("model"), cfg["openai"].get("model"))
    engine = Engine(JevClient(cfg["jev"]), LLMClient(cfg["openai"]))
    tks = store.list_tasks()
    log.info("engine ready | %d task(s): %s", len(tks), ", ".join(t["id"] for t in tks) or "(none)")
    return engine, cfg


ENGINE, CFG = build_engine()


def _reload_engine():
    """Rebuild the engine + clients from config.json (after a settings change)."""
    global ENGINE, CFG
    ENGINE, CFG = build_engine()


def _config_view():
    """Redacted config for the settings UI (never returns raw keys)."""
    raw = config_mod.load_raw()
    def keyinfo(section):
        k = raw.get(section, {}).get("api_key") or ""
        ok = bool(k) and not config_mod.is_placeholder(k)
        return {"set": ok, "hint": ("…" + k[-4:]) if ok and len(k) >= 4 else ""}
    g = lambda s, f, d="": raw.get(s, {}).get(f, d)
    return {
        "openai": {"model": g("openai", "model"), "base_url": g("openai", "base_url"), "key": keyinfo("openai")},
        "jev": {"model": g("jev", "model"), "base_url": g("jev", "base_url"), "key": keyinfo("jev")},
        "logging": {"console_level": g("logging", "console_level", "INFO")},
        "tools": raw.get("tools", {}) or {},
        "server": raw.get("server", {}),
    }


def _apply_config(body):
    """Apply only the provided, non-empty fields to config.json (keys never blanked)."""
    raw = config_mod.load_raw()
    def setif(section, field, val):
        if val is not None and str(val).strip() != "":
            raw.setdefault(section, {})[field] = str(val).strip()
    setif("openai", "model", body.get("openai_model"))
    setif("jev", "model", body.get("jev_model"))
    setif("openai", "api_key", body.get("openai_key"))
    setif("jev", "api_key", body.get("jev_key"))
    setif("logging", "console_level", body.get("console_level"))
    for k, v in (body.get("tools") or {}).items():
        if str(v).strip() != "":
            raw.setdefault("tools", {})[k] = str(v).strip()
    config_mod.save_raw(raw)


def _resolve_bundle(contract, bundle):
    """Resolve file-typed port values (asset_id or ref) to full asset refs."""
    out = dict(bundle or {})
    by_type = {p["name"]: p["type"] for p in contract.get("inputs", [])}
    for name, val in list(out.items()):
        if by_type.get(name) in adapters.ASSET_INPUT_TYPES:
            if isinstance(val, str):
                ref = assets.get_ref(val)
                if ref is None:
                    raise ValueError(f"unknown asset '{val}' for port '{name}'")
                out[name] = ref
            elif isinstance(val, dict) and val.get("asset_id") and "sha256" not in val:
                out[name] = assets.get_ref(val["asset_id"]) or val
    return out


def _parse_multipart(content_type, body):
    """Minimal multipart/form-data parser (stdlib only, bytes-safe — cgi is gone in 3.13).
    Returns a list of parts: {name, filename, content_type, data(bytes)}."""
    m = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type)
    if not m:
        raise ValueError("multipart request is missing a boundary")
    delim = b"--" + (m.group(1) or m.group(2)).strip().encode()
    parts = []
    for seg in body.split(delim)[1:]:
        if seg[:2] == b"--":            # closing boundary "--<boundary>--"
            break
        if seg[:2] == b"\r\n":          # CRLF that follows each delimiter
            seg = seg[2:]
        head_end = seg.find(b"\r\n\r\n")
        if head_end == -1:
            continue
        headers = seg[:head_end].decode("utf-8", "replace")
        data = seg[head_end + 4:]
        if data.endswith(b"\r\n"):      # CRLF before the next delimiter
            data = data[:-2]
        name = filename = ctype = None
        for line in headers.split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition:"):
                nm = re.search(r'name="([^"]*)"', line)
                fn = re.search(r'filename="([^"]*)"', line)
                name = nm.group(1) if nm else name
                filename = fn.group(1) if fn else filename
            elif low.startswith("content-type:"):
                ctype = line.split(":", 1)[1].strip()
        if name is not None:
            parts.append({"name": name, "filename": filename, "content_type": ctype, "data": data})
    return parts


def _bundle_from_multipart(contract, parts):
    """Assemble a run bundle from multipart parts, interpreting each field BY THE TASK
    CONTRACT: a file-typed port -> stored asset ref; a json port -> parsed JSON; else text."""
    by_name = {}
    for p in parts:
        by_name.setdefault(p["name"], p)
    bundle = {}
    for port in contract.get("inputs", []):
        name, t = port["name"], port["type"]
        part = by_name.get(name)
        if part is None:
            raise ValueError(f"missing input port '{name}' ({t})")
        if t in adapters.ASSET_INPUT_TYPES:
            if not part.get("filename"):
                raise ValueError(f"input port '{name}' ({t}) expects an uploaded file")
            bundle[name] = assets.put(part["data"], name=part["filename"],
                                      mime=part.get("content_type") or "application/octet-stream")
        elif t == "json":
            txt = part["data"].decode("utf-8", "replace").strip()
            try:
                bundle[name] = json.loads(txt) if txt else None
            except json.JSONDecodeError as e:
                raise ValueError(f"input port '{name}' (json) is not valid JSON: {e}")
        else:
            bundle[name] = part["data"].decode("utf-8", "replace")
    return bundle


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.debug("http: " + fmt, *args)

    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _task_or_400(self, body):
        task_id = body.get("task", "")
        if not store.task_exists(task_id):
            self._send(400, {"error": f"unknown task '{task_id}'"})
            return None
        return task_id

    def _run_multipart(self):
        """POST /api/run as multipart/form-data: a `task` field plus one field per input
        port (files inline). One call, no separate /api/upload; the contract decides how
        each field is read. Everything after building the bundle is the normal run path."""
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            parts = _parse_multipart(self.headers.get("Content-Type", ""), raw)
            tf = next((p for p in parts if p["name"] == "task"), None)
            task_id = tf["data"].decode("utf-8", "replace").strip() if tf else ""
            if not store.task_exists(task_id):
                self._send(400, {"error": f"unknown task '{task_id}'"}); return
            contract = store.load_task(task_id).get("contract", {})
            bundle = _resolve_bundle(contract, _bundle_from_multipart(contract, parts))
        except ValueError as e:
            self._send(400, {"error": str(e)}); return
        log.info("user ran one bundle (multipart): task='%s' | %d field(s)", task_id, len(parts))
        with _lock:
            rec = ENGINE.run_bundle(task_id, bundle, shape_after=True)
        self._send(200, {"records": [rec], "metrics": ENGINE.metrics(task_id)})

    # ------------------------------------------------------------- GET
    def do_GET(self):
        url = urlparse(self.path)
        (log.debug if url.path in ("/api/state", "/", "/index.html") else log.info)(
            "GET %s from %s", self.path, self.client_address[0])
        if url.path in ("/", "/index.html"):
            with open(UI_PATH, "rb") as f:
                self._send(200, f.read(), "text/html")
        elif url.path == "/api/capabilities":
            self._send(200, {
                "input_types": adapters.INPUT_TYPES,
                "output_types": adapters.OUTPUT_TYPES,
                "roles": ["stream", "constant", "container"],
                "tools": tools.catalog(),
            })
        elif url.path == "/api/tasks":
            self._send(200, {"tasks": store.list_tasks()})
        elif url.path == "/api/config":
            self._send(200, _config_view())
        elif url.path == "/api/state":
            task_id = (parse_qs(url.query).get("task") or [""])[0]
            tasks = store.list_tasks()
            if not store.task_exists(task_id):
                task_id = tasks[0]["id"] if tasks else None
            if task_id is None:
                self._send(200, {"tasks": [], "task": None,
                                 "models": {"jev": CFG["jev"].get("model"), "openai": CFG["openai"].get("model")}})
                return
            meta = store.load_task(task_id)
            tree, version = store.load_tree(task_id)
            self._send(200, {
                "task": task_id, "tasks": tasks,
                "contract": meta.get("contract", {"inputs": [], "outputs": []}),
                "pinned": meta.get("pinned", False),
                "models": {"jev": CFG["jev"].get("model"), "openai": CFG["openai"].get("model")},
                "tree": tree, "version": version, "versions": store.list_versions(task_id),
                "runs": store.load_runs(task_id, limit=30), "metrics": ENGINE.metrics(task_id),
                "prompts": {"architect": ARCHITECT_SYSTEM, "shaper": SHAPER_SYSTEM, "generator": GENERATOR_SYSTEM},
            })
        elif url.path == "/api/tree":
            qs = parse_qs(url.query)
            task_id = (qs.get("task") or [""])[0]
            if not store.task_exists(task_id):
                self._send(400, {"error": f"unknown task '{task_id}'"})
                return
            tree, ver = store.load_tree(task_id, (qs.get("version") or [None])[0])
            self._send(200, {"version": ver, "tree": tree})
        elif url.path == "/api/download":
            ref = assets.get_ref((parse_qs(url.query).get("asset") or [""])[0])
            if ref is None:
                self._send(404, {"error": "unknown asset"})
                return
            data = assets.get_bytes(ref["asset_id"])
            self.send_response(200)
            self.send_header("Content-Type", ref.get("mime", "application/octet-stream"))
            self.send_header("Content-Disposition", f'attachment; filename="{ref.get("name","download")}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send(404, {"error": "not found"})

    # ------------------------------------------------------------- POST
    def do_POST(self):
        try:
            if self.path == "/api/upload":
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length)
                ref = assets.put(data, name=self.headers.get("X-Filename", "upload.bin"),
                                 mime=self.headers.get("Content-Type", "application/octet-stream"))
                self._send(200, {"asset": ref})
                return

            if self.path == "/api/run" and self.headers.get("Content-Type", "").startswith("multipart/"):
                log.info("POST %s (multipart) from %s", self.path, self.client_address[0])
                self._run_multipart()
                return

            body = self._body()
            log.info("POST %s from %s", self.path, self.client_address[0])

            if self.path == "/api/tasks":
                name = (body.get("name") or "").strip()
                description = (body.get("description") or "").strip()
                inputs = body.get("inputs") or []
                outputs = body.get("outputs") or []
                if not name or not description or not inputs or not outputs:
                    self._send(400, {"error": "name, description, at least one input port and one output port are required"})
                    return
                contract = {"inputs": inputs, "outputs": outputs}
                log.info("user creating task '%s' (%d in, %d out)", name, len(inputs), len(outputs))
                with _lock:
                    task_id, how = ENGINE.create_task(name, description, contract, body.get("examples") or [])
                self._send(200, {"task": task_id, "bootstrapped_by": how})

            elif self.path == "/api/run":
                # One bundle of inputs = one loop.
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                contract = store.load_task(task_id).get("contract", {})
                bundle = _resolve_bundle(contract, body.get("bundle") or {})
                log.info("user ran one bundle: task='%s'", task_id)
                with _lock:
                    rec = ENGINE.run_bundle(task_id, bundle, shape_after=True)
                self._send(200, {"records": [rec], "metrics": ENGINE.metrics(task_id)})

            elif self.path == "/api/generate":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                count = max(1, min(int(body.get("count", 5)), 20))
                with _lock:
                    self._send(200, {"bundles": ENGINE.new_inputs(task_id, count)})

            elif self.path == "/api/stream":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                count = max(1, min(int(body.get("count", 5)), 20))
                log.info("user auto-streamed %d loop(s): task='%s'", count, task_id)
                with _lock:
                    bundles = ENGINE.new_inputs(task_id, count)
                    records = [ENGINE.run_bundle(task_id, b, shape_after=True) for b in bundles]
                self._send(200, {"bundles": bundles, "records": records, "metrics": ENGINE.metrics(task_id)})

            elif self.path == "/api/shape":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                with _lock:
                    self._send(200, {"shaping": ENGINE.shape(task_id), "metrics": ENGINE.metrics(task_id)})

            elif self.path == "/api/compile":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                meta = store.load_task(task_id)
                tree, version = store.load_tree(task_id)
                errs = tree_mod.compile_tree(tree, meta.get("contract", {}))
                self._send(200, {"version": version, "ok": not errs, "errors": errs})

            elif self.path == "/api/rollback":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                meta = store.set_active(task_id, body["version"], pin=body.get("pin"))
                self._send(200, {"active_version": meta.get("active_version"), "pinned": meta.get("pinned")})

            elif self.path == "/api/update-task":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                meta = store.update_task(task_id, name=body.get("name"), description=body.get("description"))
                self._send(200, {"task": task_id, "name": meta.get("name"), "description": meta.get("description")})

            elif self.path == "/api/delete-task":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                with _lock:
                    store.delete_task(task_id)
                self._send(200, {"deleted": task_id, "tasks": store.list_tasks()})

            elif self.path == "/api/config":
                with _lock:
                    _apply_config(body)
                    _reload_engine()
                self._send(200, {"ok": True, "config": _config_view(),
                                 "models": {"jev": CFG["jev"].get("model"), "openai": CFG["openai"].get("model")}})

            elif self.path == "/api/pin-fixture":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                runs = store.load_runs(task_id, limit=200)
                target = next((r for r in runs if r["ts"] == body.get("run_ts")), runs[-1] if runs else None)
                if not target:
                    self._send(400, {"error": "no run to pin"})
                    return
                store.pin_fixture(task_id, {"inputs": target.get("raw_inputs") or target.get("inputs"),
                                            "expected_pass": target["evaluation"]["passed"]})
                self._send(200, {"pinned_fixtures": len(store.load_fixtures(task_id))})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            log.exception("POST %s failed: %s", self.path, e)
            self._send(500, {"error": str(e)})


def main():
    host, port = CFG["server"]["host"], CFG["server"]["port"]
    print(f"CloseLoop running at http://{host}:{port}  [jev: {CFG['jev'].get('model')} | openai: {CFG['openai'].get('model')}]")
    log.info("serving on http://%s:%d", host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()

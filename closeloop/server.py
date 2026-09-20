"""Zero-dependency HTTP server: serves the UI and a small JSON API.

  GET  /                    -> ui/index.html
  GET  /api/tasks           -> list of tasks
  POST /api/tasks           -> create a task from a plain-language definition (S2 bootstraps the tree)
  GET  /api/state?task=<id> -> mode, tree, versions, recent runs, metrics for one task
  POST /api/run             -> {"task": id, "inputs": [str,...]}  one loop per DISTINCT input
  POST /api/stream          -> {"task": id, "count": n}  generate n new instances & loop each
  POST /api/shape           -> {"task": id}  force a shaping pass now
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config as config_mod, logsetup, store, tree as tree_mod
from .config import ROOT
from .engine import Engine, GENERATOR_SYSTEM, SHAPER_SYSTEM, BOOTSTRAP_SYSTEM
from .jev import JevClient
from .llm import LLMClient

UI_PATH = os.path.join(ROOT, "ui", "index.html")
SEED_PATH = os.path.join(ROOT, "trees", "seed.json")

_lock = threading.Lock()  # serialize runs/shaping (open question 5)
log = logsetup.get("server")


def build_engine():
    cfg = config_mod.load()
    logsetup.setup_logging(cfg)  # configure sinks before anything logs meaningfully
    log.info("starting CloseLoop | jev=%s openai=%s | model jev=%s openai=%s",
             "MOCK" if cfg["jev"]["mock"] else "LIVE",
             "MOCK" if cfg["openai"]["mock"] else "LIVE",
             cfg["jev"].get("model"), cfg["openai"].get("model"))
    engine = Engine(JevClient(cfg["jev"]), LLMClient(cfg["openai"]))
    # First launch: register the demo task from the seed tree.
    if not store.list_tasks():
        with open(SEED_PATH, "r", encoding="utf-8") as f:
            seed = json.load(f)
        task_id = store.create_task("Customer support demo",
                                    {"description": seed.get("task", {}).get("kind", "demo")})
        store.save_tree(task_id, seed, "seed tree (bootstrap)")
    log.info("engine ready | %d task(s): %s", len(store.list_tasks()),
             ", ".join(t["id"] for t in store.list_tasks()))
    return engine, cfg


ENGINE, CFG = build_engine()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # route http.server's own logging to DEBUG
        log.debug("http: " + fmt, *args)

    # ------------------------------------------------------------- helpers
    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
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

    # ------------------------------------------------------------- routes
    def do_GET(self):
        url = urlparse(self.path)
        # /api/state and the UI shell are polled frequently -> DEBUG; rest INFO.
        (log.debug if url.path in ("/api/state", "/", "/index.html") else log.info)(
            "GET %s from %s", self.path, self.client_address[0])
        if url.path in ("/", "/index.html"):
            with open(UI_PATH, "rb") as f:
                self._send(200, f.read(), "text/html")
        elif url.path == "/api/tasks":
            self._send(200, {"tasks": store.list_tasks()})
        elif url.path == "/api/state":
            task_id = (parse_qs(url.query).get("task") or [""])[0]
            tasks = store.list_tasks()
            if not store.task_exists(task_id):
                task_id = tasks[0]["id"] if tasks else None
            if task_id is None:
                self._send(200, {"tasks": [], "task": None})
                return
            tree, version = store.load_tree(task_id)
            self._send(200, {
                "task": task_id,
                "tasks": tasks,
                "mode": {"jev_mock": ENGINE.jev.mock, "openai_mock": ENGINE.llm.mock,
                         "jev_fallback": ENGINE.jev.fallback_reason,
                         "openai_fallback": ENGINE.llm.fallback_reason},
                "tree": tree,
                "version": version,
                "versions": store.list_versions(task_id),
                "runs": store.load_runs(task_id, limit=30),
                "metrics": ENGINE.metrics(task_id),
                "prompts": {"shaper": SHAPER_SYSTEM, "generator": GENERATOR_SYSTEM,
                            "architect": BOOTSTRAP_SYSTEM},
            })
        elif url.path == "/api/tree":
            # Fetch a specific historical tree version (so the graph can show
            # the workflow AS IT WAS when a past loop ran).
            qs = parse_qs(url.query)
            task_id = (qs.get("task") or [""])[0]
            version = (qs.get("version") or [None])[0]
            if not store.task_exists(task_id):
                self._send(400, {"error": f"unknown task '{task_id}'"})
                return
            tree, ver = store.load_tree(task_id, version)
            self._send(200, {"version": ver, "tree": tree})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._body()
            log.info("POST %s from %s", self.path, self.client_address[0])
            if self.path == "/api/tasks":
                name = body.get("name", "").strip()
                description = body.get("description", "").strip()
                output_desc = body.get("output", "").strip()
                if not name or not description or not output_desc:
                    log.warning("create_task rejected: missing required fields")
                    self._send(400, {"error": "name, description and output are required"})
                    return
                examples = [s.strip() for s in body.get("examples", []) if isinstance(s, str) and s.strip()]
                log.info("user creating task '%s'", name)
                with _lock:
                    task_id, how = ENGINE.create_task(name, description, output_desc,
                                                      body.get("metric", "").strip(), examples)
                self._send(200, {"task": task_id, "bootstrapped_by": how})
            elif self.path == "/api/run":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                inputs = [s.strip() for s in body.get("inputs", []) if isinstance(s, str) and s.strip()]
                if not inputs:
                    self._send(400, {"error": "inputs (one task instance per loop) required"})
                    return
                inputs = inputs[:50]
                log.info("user ran batch: task='%s' %d input(s)", task_id, len(inputs))
                with _lock:
                    records = [ENGINE.run_once(task_id, t, shape_after=True) for t in inputs]
                self._send(200, {"records": records, "metrics": ENGINE.metrics(task_id)})
            elif self.path == "/api/generate":
                # Generate N new same-kind inputs WITHOUT running them, so the
                # client can drive loops one at a time and show real progress.
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                count = max(1, min(int(body.get("count", 5)), 20))
                log.info("user requested %d generated input(s): task='%s'", count, task_id)
                with _lock:
                    inputs = ENGINE.new_inputs(task_id, count)
                self._send(200, {"inputs": inputs})
            elif self.path == "/api/stream":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                count = max(1, min(int(body.get("count", 5)), 20))
                log.info("user auto-streamed %d loop(s): task='%s'", count, task_id)
                with _lock:
                    inputs = ENGINE.new_inputs(task_id, count)
                    records = [ENGINE.run_once(task_id, t, shape_after=True) for t in inputs]
                self._send(200, {"inputs": inputs, "records": records, "metrics": ENGINE.metrics(task_id)})
            elif self.path == "/api/shape":
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                log.info("user forced shaping: task='%s'", task_id)
                with _lock:
                    result = ENGINE.shape(task_id)
                self._send(200, {"shaping": result, "metrics": ENGINE.metrics(task_id)})
            elif self.path == "/api/compile":
                # Compile + validate the task's CURRENT tree (graph structure,
                # reachability, tsc type-check of all code). Empty errors = clean.
                task_id = self._task_or_400(body)
                if task_id is None:
                    return
                log.info("user compiled tree: task='%s'", task_id)
                current_tree, version = store.load_tree(task_id)
                errors = tree_mod.compile_tree(current_tree)
                self._send(200, {"version": version, "ok": not errors, "errors": errors})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            log.exception("POST %s failed: %s", self.path, e)
            self._send(500, {"error": str(e)})


def main():
    host = CFG["server"]["host"]
    port = CFG["server"]["port"]
    mode = []
    mode.append("Jev: " + ("MOCK (no key)" if CFG["jev"]["mock"] else "LIVE"))
    mode.append("OpenAI: " + ("MOCK (no key)" if CFG["openai"]["mock"] else "LIVE"))
    banner = f"CloseLoop prototype running at http://{host}:{port}  [{' | '.join(mode)}]"
    print(banner)
    log.info("serving on http://%s:%d", host, port)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()

"""The CloseLoop engine (multi-port): one loop = run bundle -> evaluate -> shape.

A task's contract is a set of typed INPUT ports and OUTPUT ports (§17). Each loop
consumes one bundle of inputs and fills the output ports; the tree S2 designs
lives between them. The contract is static (only the user changes it); the shaper
edits only the tree.
"""
import json
import re
import time

from . import adapters, logsetup, schema, store, tools, tree as tree_mod

log = logsetup.get("engine")

ARCHITECT_SYSTEM = """You are the ARCHITECT (System 2) of a CloseLoop self-improving system.
Given a task description and a FIXED multi-port contract (input ports + output ports), design the
INITIAL decision/action tree that reads the inputs and fills EVERY required output port.
Rules:
- Use the capability catalog provided: node kinds jev/llm/code/tool, and the available tools.
- jev = judgement only (no generation/among). llm = the only generator (returns a JSON object).
  code = deterministic TypeScript glue. tool = host ops (ocr/vision/transcribe/render_doc/image_gen/tts/chart/http).
- Fill each output port via a node with output_port="<portName>" (llm/tool) or by writing ctx.outputs["<portName>"] in code.
- Also design "evaluation": for each output port, a check — a TypeScript `evaluate(output: Json, inputs: Record<string,Json>): Verdict`
  under evaluation[<portName>].code, and/or an s1_battery of noul questions (over state {inputs, output}) with s1_pass_threshold.
  Verdict is EXACTLY `{ passed: boolean; score?: number; notes?: string }` — return e.g. `return { passed: true, score: 1, notes: "ok" };` (use `passed`, not `ok`).
- You may NOT add/remove/rename/retype ports (the contract is fixed).
COMPILE RULES (a violation rejects the whole tree):
- In code/evaluation TypeScript, do NOT declare `type Json`, `type Ctx`, or `type Verdict` — they are predeclared.
- Every node must be reachable from entry (no orphans). Every required output port must be filled by some node.
- jev choice `criteria` must be an object {option: description}; score `criteria` a list of level strings.
Respond with STRICT JSON only: {"tree": { "entry": id, "nodes": {...}, "evaluation": {...}, "task": {"kind": "...", "sample_inputs": [ {portName: value}, ... ]} }}"""

SHAPER_SYSTEM = """You are the SHAPER (System 2) of a CloseLoop self-improving system.
You receive the FIXED contract, the current tree, and recent run records (each with the actual inputs,
the ACTUAL outputs produced, a node trace, and per-output-port evaluation scores). Diagnose from what
really happened, not guesses. If results are good and stable, DO NOT change the tree.
You may edit the tree only — rewrite jev questions/criteria/thresholds, add/remove/reorder nodes,
rewrite code/llm/tool nodes, adjust the per-output evaluation. You may NOT change the contract (ports/types/metric).
Know your tools: jev JUDGES meaning; llm is the ONLY generator; code is deterministic glue; tool runs host ops.
If an output reads poorly or a quality check keeps failing, fix the llm node (or its prompt), not with more code.
Respond with STRICT JSON only:
  {"shape": false, "reason": "..."}  OR
  {"shape": true, "reason": "...", "tree": { ...complete new tree... }}"""

GENERATOR_SYSTEM = """You generate NEW task input bundles for a CloseLoop system (R-3.0: every loop a
different instance of the same kind). Given the input ports and examples, produce `count` bundles. Each
bundle is an object mapping each NON-file input port name to a realistic, varied value (text/json). Do not
invent values for file-typed ports (table/document/image/audio). Respond with STRICT JSON only:
{"bundles": [ {portName: value, ...}, ... ]}"""


class Engine:
    def __init__(self, jev_client, llm_client):
        self.jev = jev_client
        self.llm = llm_client

    # ------------------------------------------------------ task creation
    def create_task(self, name, description, contract, examples=None):
        """Bootstrap a task from a description + a fixed multi-port contract.
        contract = {inputs:[{name,type,settings}], outputs:[{name,type,settings,metric,required}]}.
        One bundle of inputs = one loop. Returns (task_id, how)."""
        _defaults(contract)
        catalog = schema.build_catalog(contract, tools.catalog())
        form = {"task_description": description, "contract": contract, "examples": examples or []}
        log.info("create_task '%s' | %d input port(s), %d output port(s)",
                 name, len(contract.get("inputs", [])), len(contract.get("outputs", [])))
        tree, how = None, "template"
        user = catalog + "\n\nTASK:\n" + json.dumps(form, indent=1, default=str)
        for attempt in range(2):  # one self-repair retry: feed compile errors back to S2
            try:
                text, _m = self.llm.complete(ARCHITECT_SYSTEM, user, force_json=True)
                candidate = json.loads(text).get("tree")
                errs = tree_mod.compile_tree(candidate, contract)
                if not errs:
                    tree, how = candidate, "s2"
                    log.info("architect designed tree: %d nodes (attempt %d)", len(candidate.get("nodes", {})), attempt + 1)
                    break
                log.warning("architect attempt %d failed compile gate: %s", attempt + 1, logsetup.preview("; ".join(errs), 200))
                user = (catalog + "\n\nTASK:\n" + json.dumps(form, indent=1, default=str)
                        + "\n\nYOUR PREVIOUS TREE FAILED THE COMPILE GATE:\n" + "\n".join(errs)
                        + "\nReturn a corrected COMPLETE tree that fixes these.")
            except Exception as e:
                log.warning("architect attempt %d error: %s", attempt + 1, logsetup.preview(str(e), 160))
        if tree is None:
            raise RuntimeError("The architect (S2) could not produce a valid workflow for this contract "
                               "after 2 attempts. No placeholder is used — please retry or refine the task/ports.")
        task_id = store.create_task(name, {"description": description, "contract": contract})
        store.save_tree(task_id, tree, "bootstrap (S2)")
        log.info("task created: id='%s' by=%s", task_id, how)
        return task_id, how

    # ---------------------------------------------------------------- run
    def run_bundle(self, task_id, bundle, shape_after=True):
        """Run one input bundle (each input port → a value/asset) through the tree."""
        meta = store.load_task(task_id)
        contract = meta.get("contract", {})
        tree, version = store.load_tree(task_id)
        inputs_json = self._load_inputs(contract, bundle)
        rec = self._execute(task_id, tree, version, contract, inputs_json)
        if shape_after:
            rec["shaping"] = self.shape(task_id)
        store.append_run(task_id, rec)
        return rec

    def _load_inputs(self, contract, bundle):
        inputs_json = {}
        for p in contract.get("inputs", []):
            val = (bundle or {}).get(p["name"])
            it = adapters.load_port(p, val)
            inputs_json[p["name"]] = it["json"]
        return inputs_json

    def _execute(self, task_id, tree, version, contract, inputs_json):
        started = time.time()
        runner = tree_mod.TreeRunner(tree, self.jev, self.llm)
        outcome = runner.run(inputs_json)
        evaluation = self.evaluate(tree, contract, inputs_json, outcome["outputs"])
        materialized = self.materialize(contract, outcome["outputs"])
        duration = int((time.time() - started) * 1000)
        path = " -> ".join(t["node"] for t in outcome["trace"])
        (log.info if evaluation["passed"] else log.warning)(
            "loop: task='%s' %s %s | escalated=%s | %dms | %s", task_id, version,
            "PASS" if evaluation["passed"] else "FAIL", outcome["escalated"], duration, path)
        return {
            "ts": int(started), "tree_version": version,
            "inputs": {k: logsetup.preview(v, 300) for k, v in inputs_json.items()},
            "raw_inputs": inputs_json,
            "raw_outputs": outcome["outputs"],
            "outputs": materialized,
            "error": outcome["error"], "escalated": outcome["escalated"],
            "trace": outcome["trace"], "evaluation": evaluation, "duration_ms": duration,
        }

    # --------------------------------------------------------------- eval
    def evaluate(self, tree, contract, inputs_json, outputs):
        """Per-output-port evaluation (§24). Task passes iff all required outputs pass."""
        ev = tree.get("evaluation", {}) or {}
        report = {"passed": True, "outputs": {}}
        for p in contract.get("outputs", []):
            name = p["name"]
            val = outputs.get(name)
            parts = {}
            ok = True
            if val is None:
                parts["present"] = {"passed": not p.get("required", True), "notes": "output not produced"}
                ok = not p.get("required", True)
            spec = ev.get(name, {})
            if val is not None and spec.get("code"):
                try:
                    verdict = tools_eval_code(spec["code"], val, inputs_json)
                    parts["code"] = verdict
                    ok = ok and bool(verdict.get("passed", False))
                except Exception as e:
                    parts["code"] = {"passed": False, "notes": f"validator crashed: {e}"}
                    ok = False
            if val is not None and spec.get("s1_battery"):
                thr = spec.get("s1_pass_threshold", 0.6)
                try:
                    answers, _m = self.jev.system_one({"inputs": inputs_json, "output": val}, spec["s1_battery"])
                    checks = {}
                    for k, a in answers.items():
                        if a.get("type") == "noul":
                            checks[k] = {"noul": a["noul"], "passed": a["noul"] >= thr}
                            ok = ok and a["noul"] >= thr
                        else:
                            checks[k] = a
                    parts["s1_battery"] = {"threshold": thr, "checks": checks}
                except Exception as e:
                    parts["s1_battery"] = {"passed": False, "notes": f"S1 battery failed: {e}"}
                    ok = False
            if val is not None and spec.get("s2_review"):
                try:
                    q = ("Judge this output for the task. Output: %s\nInputs: %s\nCriterion: %s\n"
                         "Respond JSON {\"passed\": bool, \"notes\": \"...\"}."
                         % (json.dumps(val)[:1500], json.dumps(inputs_json)[:1000], spec["s2_review"]))
                    txt, _m = self.llm.complete("You are a strict reviewer.", q, force_json=True)
                    v = json.loads(txt)
                    parts["s2_review"] = v
                    ok = ok and bool(v.get("passed", False))
                except Exception as e:
                    parts["s2_review"] = {"passed": False, "notes": f"S2 review failed: {e}"}
                    ok = False
            report["outputs"][name] = {"passed": ok, "parts": parts}
            if p.get("required", True):
                report["passed"] = report["passed"] and ok
        return report

    # ---------------------------------------------------------- materialize
    def materialize(self, contract, outputs):
        out = {}
        for p in contract.get("outputs", []):
            val = outputs.get(p["name"])
            if val is None:
                out[p["name"]] = None
                continue
            try:
                out[p["name"]] = adapters.materialize_port(p, val)
            except Exception as e:
                out[p["name"]] = {"error": str(e), "value": val}
        return out

    # -------------------------------------------------------------- shape
    def shape(self, task_id):
        meta = store.load_task(task_id)
        contract = meta.get("contract", {})
        tree, version = store.load_tree(task_id)
        runs = store.load_runs(task_id, limit=12)
        fails = [r for r in runs if not r["evaluation"]["passed"]]
        passes = [r for r in runs if r["evaluation"]["passed"]]
        chosen = (fails[-4:] + passes[-2:]) or runs[-4:]
        chosen.sort(key=lambda r: r["ts"])
        digest = [_run_evidence(r) for r in chosen]
        catalog = schema.build_catalog(contract, tools.catalog())
        user = ("%s\n\nCURRENT TREE (%s):\n%s\n\nRECENT RUNS (read the outputs and failing checks):\n%s"
                % (catalog, version, json.dumps(_no_meta(tree), indent=1), json.dumps(digest, indent=1, default=str)))
        try:
            text, _m = self.llm.complete(SHAPER_SYSTEM, user, force_json=True)
        except Exception as e:
            log.error("shape: shaper unavailable: %s", e)
            return {"shaped": False, "reason": f"shaper unavailable: {e}"}
        try:
            decision = json.loads(text)
        except json.JSONDecodeError:
            return {"shaped": False, "reason": "shaper returned non-JSON"}
        if not decision.get("shape"):
            log.info("shape: task='%s' DECLINED — %s", task_id, logsetup.preview(decision.get("reason", ""), 160))
            return {"shaped": False, "reason": decision.get("reason", "")}
        new_tree = decision.get("tree")
        errs = tree_mod.compile_tree(new_tree, contract)   # compile gate (static contract via `contract`)
        if errs:
            log.warning("shape: edit REJECTED by compile gate: %s", logsetup.preview("; ".join(errs), 300))
            return {"shaped": False, "reason": "compile gate: " + " | ".join(errs)[:400]}
        reg = self._regression_ok(task_id, new_tree, contract)   # pinned-fixture regression (C7)
        if not reg["ok"]:
            log.warning("shape: edit REJECTED by regression: %s", reg["reason"])
            return {"shaped": False, "reason": "regression: " + reg["reason"]}
        new_version = store.save_tree(task_id, new_tree, decision.get("reason", "shaper edit"))
        log.info("shape: task='%s' EDITED %s -> %s", task_id, version, new_version)
        return {"shaped": True, "reason": decision.get("reason", ""), "new_version": new_version}

    def _regression_ok(self, task_id, new_tree, contract):
        """Dry-run a shaped tree against pinned fixtures; reject if a previously
        good fixture now fails (C7). No fixtures → pass."""
        fixtures = store.load_fixtures(task_id)
        if not fixtures:
            return {"ok": True, "reason": "no fixtures"}
        runner = tree_mod.TreeRunner(new_tree, self.jev, self.llm)
        for fx in fixtures:
            if not fx.get("expected_pass", True):
                continue
            try:
                outcome = runner.run(fx["inputs"])
                ev = self.evaluate(new_tree, contract, fx["inputs"], outcome["outputs"])
                if not ev["passed"]:
                    return {"ok": False, "reason": f"fixture '{logsetup.preview(fx['inputs'],60)}' regressed"}
            except Exception as e:
                return {"ok": False, "reason": f"fixture crashed: {e}"}
        return {"ok": True, "reason": f"{len(fixtures)} fixture(s) held"}

    # --------------------------------------------------------- task stream
    def new_inputs(self, task_id, n):
        """Generate N new input bundles (non-file ports) for auto-stream."""
        meta = store.load_task(task_id)
        contract = meta.get("contract", {})
        tree, _ = store.load_tree(task_id)
        examples = (tree.get("task") or {}).get("sample_inputs", [])
        ports = [p for p in contract.get("inputs", []) if p["type"] in ("text", "json")]
        user = json.dumps({"input_ports": [{"name": p["name"], "type": p["type"]} for p in ports],
                           "examples": examples[:6], "count": n})
        text, _m = self.llm.complete(GENERATOR_SYSTEM, user, force_json=True)
        bundles = [b for b in json.loads(text).get("bundles", []) if isinstance(b, dict) and b]
        if not bundles:
            raise RuntimeError("the generator (S2) returned no input bundles")
        log.info("new_inputs: task='%s' generated %d bundle(s)", task_id, len(bundles[:n]))
        return bundles[:n]

    # ------------------------------------------------------------ metrics
    @staticmethod
    def metrics(task_id):
        runs = store.load_runs(task_id)
        if not runs:
            return {"runs": 0, "pass_rate": None, "escalation_rate": None, "shaping_edits": 0,
                    "avg_confidence": None, "cost_tokens": 0, "tree_version": store.active_version(task_id)}
        window = runs[-20:]
        confs, toks = [], 0
        for r in runs:
            for t in r.get("trace", []):
                for a in (t.get("answers") or {}).values():
                    if isinstance(a, dict) and "confidence" in a:
                        confs.append(a["confidence"])
                u = (t.get("meta") or {}).get("usage") or {}
                toks += u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("total_tokens", 0)
        shaped = sum(1 for r in runs if (r.get("shaping") or {}).get("shaped"))
        return {
            "runs": len(runs),
            "pass_rate": round(sum(1 for r in window if r["evaluation"]["passed"]) / len(window), 2),
            "escalation_rate": round(sum(1 for r in window if r["escalated"]) / len(window), 2),
            "shaping_edits": shaped,
            "avg_confidence": round(sum(confs) / len(confs), 3) if confs else None,
            "cost_tokens": toks,
            "tree_version": store.active_version(task_id),
        }


# ------------------------------------------------------------------ helpers
def tools_eval_code(code, output, inputs_json):
    from . import tscode
    return tscode.run_evaluate(code, output, inputs_json)


def _defaults(contract):
    # one bundle = one loop: input ports have no role; ensure outputs default to required
    for p in contract.get("inputs", []):
        p.pop("role", None)
    for p in contract.get("outputs", []):
        p.setdefault("required", True)


def _no_meta(tree):
    return {k: v for k, v in tree.items() if k != "_meta"}


def _run_evidence(r):
    trace = []
    for t in r.get("trace", []):
        step = {"node": t["node"], "kind": t["kind"]}
        if t.get("answers"):
            step["decided"] = {k: (a.get("choice") or a.get("noul") or a.get("score"))
                               for k, a in t["answers"].items()}
        if t.get("routed_to"):
            step["routed_to"] = t["routed_to"]
        if t.get("gate_fired"):
            step["gate_fired"] = t["gate_fired"]
        if t.get("output_preview"):
            step["produced"] = t["output_preview"]
        if t.get("error"):
            step["error"] = t["error"]
        trace.append(step)
    return {"inputs": r.get("inputs"), "outputs": r.get("raw_outputs"),
            "passed": r["evaluation"]["passed"], "error": r.get("error"),
            "evaluation": r["evaluation"].get("outputs"), "trace": trace}

"""Decision/action tree: the artifact S2 shapes and S1 executes.

Tree JSON shape (versioned, diffable — REQUIREMENT.md R-5.5, open question 1):

{
  "entry": "<node id>",
  "nodes": {
    "<id>": {
      "kind": "jev",                       # S1 decision node
      "questions": { <Jev question map> }, # noul/choice/score, batched in ONE call
      "gates": [                           # confidence-gated escalation (R-3.2a)
        {"question": "q", "min_confidence": 0.6, "escalate_to": "<node id>"}
      ],
      "route": {                           # declarative routing — no eval() of model text
        "on": "q",                         # question key to branch on
        "branches": {"optionA": "<id>"},   # for choice questions
        "noul_threshold": 0.5,             # for noul questions:
        "if_true": "<id>", "if_false": "<id>",
        "default": "<id>"
      },
      "next": "<id>"                       # unconditional fallthrough if no route
    },
    "<id>": { "kind": "llm",               # S2 operation node (generation — Jev can't)
      "system": "...", "prompt": "... {input} {vars.x} ...",
      "output_key": "draft", "next": "<id>" },
    "<id>": { "kind": "code",              # S2-authored executable operation
      "source": "def run(ctx): ...",       # sets ctx['vars'][...] / ctx['result']
      "next": "<id>" }
  },
  "evaluation": {
    "code": "def evaluate(task_input, result): return {'passed': bool, 'score': float, 'notes': str}",
    "s1_battery": { <Jev question map over {input, result} state> },
    "s1_pass_threshold": 0.6
  }
}
"""
import json
import math
import re

from . import logsetup

log = logsetup.get("tree")

MAX_STEPS = 30

# Code nodes are S2-authored: only these modules may be imported (prototype
# sandbox; real isolation is REQUIREMENT.md open question 2).
SAFE_MODULES = {"re": re, "json": json, "math": math}


def safe_import(name, *args, **kwargs):
    if name in SAFE_MODULES:
        return SAFE_MODULES[name]
    raise ImportError(f"module '{name}' is not available inside tree code nodes (allowed: re, json, math)")


SAFE_BUILTINS = {"len": len, "str": str, "int": int, "float": float, "bool": bool,
                 "min": min, "max": max, "sum": sum, "sorted": sorted, "any": any, "all": all,
                 "list": list, "dict": dict, "set": set, "tuple": tuple, "enumerate": enumerate,
                 "zip": zip, "range": range, "abs": abs, "round": round, "isinstance": isinstance,
                 "repr": repr, "ValueError": ValueError, "Exception": Exception,
                 "__import__": safe_import}


class TreeError(Exception):
    pass


def is_legacy_python(source):
    return "def run(" in source or "def evaluate(" in source


def validate(tree):
    """Structural graph validation before a tree version is accepted (R-5.5)."""
    if not isinstance(tree, dict):
        raise TreeError("tree must be an object")
    nodes = tree.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise TreeError("tree.nodes must be a non-empty object")
    entry = tree.get("entry")
    if entry not in nodes:
        raise TreeError(f"tree.entry '{entry}' is not a node id")
    for nid, node in nodes.items():
        kind = node.get("kind")
        if kind not in ("jev", "llm", "code"):
            raise TreeError(f"node '{nid}': unknown kind '{kind}'")
        if kind == "jev" and not node.get("questions"):
            raise TreeError(f"jev node '{nid}' has no questions")
        if kind == "llm" and not node.get("prompt"):
            raise TreeError(f"llm node '{nid}' has no prompt")
        if kind == "code":
            src = node.get("source", "")
            if "function run(" not in src and "def run(" not in src:
                raise TreeError(f"code node '{nid}' must define TypeScript `function run(ctx)`")
            if not is_legacy_python(src) and ("import " in src or "require(" in src):
                raise TreeError(f"code node '{nid}': imports are not allowed in code nodes")
        for ref in _refs(node):
            if ref is not None and ref not in nodes:
                raise TreeError(f"node '{nid}' references unknown node '{ref}'")
    # reachability: every node must be reachable from entry (no orphan subgraphs)
    reachable = {entry}
    frontier = [entry]
    while frontier:
        nid = frontier.pop()
        for ref in _refs(nodes[nid]):
            if ref is not None and ref not in reachable:
                reachable.add(ref)
                frontier.append(ref)
    orphans = set(nodes) - reachable
    if orphans:
        raise TreeError(f"unreachable nodes (orphans): {', '.join(sorted(orphans))}")
    return True


def compile_tree(tree):
    """Compile the WHOLE graph: structural validation + type-check of every
    code node and the evaluation validator. Returns a list of error strings
    (empty list = tree compiles clean and may be deployed)."""
    from . import tscode
    errors = []
    try:
        validate(tree)
    except TreeError as e:
        log.warning("compile: graph validation failed: %s", e)
        return [f"graph: {e}"]
    log.debug("compile: graph valid (%d nodes) — type-checking code", len(tree.get("nodes", {})))
    for nid, node in tree.get("nodes", {}).items():
        if node.get("kind") != "code":
            continue
        src = node.get("source", "")
        if is_legacy_python(src):
            try:
                compile(src, f"<node {nid}>", "exec")
            except SyntaxError as e:
                errors.append(f"code node '{nid}' (legacy python): {e}")
        else:
            err = tscode.typecheck(src, "run")
            if err:
                errors.append(f"code node '{nid}': {err}")
    ev_code = tree.get("evaluation", {}).get("code")
    if ev_code:
        if is_legacy_python(ev_code):
            try:
                compile(ev_code, "<evaluation>", "exec")
            except SyntaxError as e:
                errors.append(f"evaluation code (legacy python): {e}")
        else:
            err = tscode.typecheck(ev_code, "eval")
            if err:
                errors.append(f"evaluation code: {err}")
    if errors:
        log.warning("compile: %d type/syntax error(s): %s", len(errors), logsetup.preview("; ".join(errors), 300))
    else:
        log.debug("compile: clean — tree may be deployed")
    return errors


def _refs(node):
    yield node.get("next")
    for g in node.get("gates", []):
        yield g.get("escalate_to")
    route = node.get("route") or {}
    yield route.get("default")
    yield route.get("if_true")
    yield route.get("if_false")
    for target in (route.get("branches") or {}).values():
        yield target


class TreeRunner:
    """Walks a tree for one task. Code owns control flow (R-2.4);
    S1 supplies judgements, S2 supplies content."""

    def __init__(self, tree, jev_client, llm_client):
        self.tree = tree
        self.jev = jev_client
        self.llm = llm_client

    def run(self, task_input):
        ctx = {"input": task_input, "vars": {}, "result": None}
        trace = []
        node_id = self.tree["entry"]
        steps = 0
        escalated = False
        log.debug("tree run start: entry=%s | input: %s", node_id, logsetup.preview(task_input, 160))

        while node_id is not None and steps < MAX_STEPS:
            steps += 1
            node = self.tree["nodes"][node_id]
            entry = {"node": node_id, "kind": node["kind"]}
            log.debug("step %d: node=%s kind=%s", steps, node_id, node["kind"])
            try:
                if node["kind"] == "jev":
                    node_id, escalation = self._run_jev(node, ctx, entry)
                    escalated = escalated or escalation
                elif node["kind"] == "llm":
                    node_id = self._run_llm(node, ctx, entry)
                elif node["kind"] == "code":
                    node_id = self._run_code(node, ctx, entry)
            except Exception as e:  # a failing node ends the run; the eval stage will flag it
                entry["error"] = str(e)
                trace.append(entry)
                log.error("node '%s' (%s) failed: %s — run aborted", entry["node"], entry["kind"], e)
                return {"result": ctx["result"], "trace": trace, "error": str(e), "escalated": escalated}
            trace.append(entry)

        if steps >= MAX_STEPS and node_id is not None:
            log.warning("tree run hit MAX_STEPS=%d (possible loop) — stopping at %s", MAX_STEPS, node_id)
        log.debug("tree run end: %d steps | escalated=%s | result: %s",
                  steps, escalated, logsetup.preview(ctx["result"], 160))
        return {"result": ctx["result"], "trace": trace, "error": None, "escalated": escalated}

    # -- S1 decision node ------------------------------------------------
    def _run_jev(self, node, ctx, entry):
        state = {"task_input": ctx["input"], **{k: v for k, v in ctx["vars"].items() if isinstance(v, (str, int, float, list, dict))}}
        answers, meta = self.jev.system_one(state, node["questions"])
        entry["state_preview"] = {k: str(v)[:220] for k, v in state.items()}
        entry["answers"] = answers
        entry["meta"] = meta

        # Confidence gates first: low confidence → escalate to S2 (R-3.2a)
        for gate in node.get("gates", []):
            ans = answers.get(gate["question"], {})
            conf = ans.get("confidence")
            if conf is None and ans.get("type") == "noul":
                conf = abs(ans["noul"] - 0.5) * 2  # distance from equiprobable
            if conf is not None and conf < gate["min_confidence"]:
                entry["gate_fired"] = {"question": gate["question"], "confidence": conf}
                log.warning("gate fired at '%s': %s confidence %.2f < %.2f — escalating to S2 node '%s'",
                            entry["node"], gate["question"], conf, gate["min_confidence"], gate["escalate_to"])
                return gate["escalate_to"], True

        route = node.get("route")
        if route:
            ans = answers.get(route["on"], {})
            if ans.get("type") == "choice":
                target = (route.get("branches") or {}).get(ans["choice"], route.get("default"))
                entry["routed_to"] = target
                log.debug("route '%s' on choice '%s'=%r -> %s", entry["node"], route["on"], ans.get("choice"), target)
                return target, False
            if ans.get("type") == "noul":
                target = route.get("if_true") if ans["noul"] >= route.get("noul_threshold", 0.5) else route.get("if_false")
                entry["routed_to"] = target
                log.debug("route '%s' on noul '%s'=%.2f (thr %.2f) -> %s", entry["node"], route["on"],
                          ans["noul"], route.get("noul_threshold", 0.5), target)
                return target, False
            if ans.get("type") == "score":
                # branches keyed by floor(score)
                target = (route.get("branches") or {}).get(str(int(ans["score"])), route.get("default"))
                entry["routed_to"] = target
                log.debug("route '%s' on score '%s'=%.2f -> %s", entry["node"], route["on"], ans.get("score", 0), target)
                return target, False
        return node.get("next"), False

    # -- S2 operation node ----------------------------------------------
    def _run_llm(self, node, ctx, entry):
        prompt = _fill(node["prompt"], ctx)
        # Every LLM node emits a JSON object (n8n-style structured data).
        system = node.get("system", "You are a helpful assistant.") + \
            "\nRespond with a single JSON object (no prose outside the JSON)."
        text, meta = self.llm.complete(system, prompt, force_json=True)
        try:
            obj = json.loads(text)
            if not isinstance(obj, dict):
                obj = {"value": obj}
        except (ValueError, TypeError):
            obj = {"text": text}  # non-JSON fallback: wrap so vars stay JSON objects
        key = node.get("output_key", "output")
        ctx["vars"][key] = obj
        if node.get("is_result", False) or node.get("next") is None:
            ctx["result"] = obj
        entry["prompt_preview"] = prompt[:400]
        entry["output_key"] = key
        entry["output_preview"] = json.dumps(obj)[:400]
        entry["meta"] = meta
        log.debug("llm node '%s' -> vars.%s (json): %s", entry["node"], key, logsetup.preview(obj, 160))
        return node.get("next")

    # -- S2-authored executable node -------------------------------------
    def _run_code(self, node, ctx, entry):
        entry["vars_before"] = {k: logsetup.preview(v, 220) for k, v in ctx["vars"].items()}
        src = node["source"]
        lang = "python(legacy)" if is_legacy_python(src) else "typescript"
        log.debug("code node '%s' exec (%s)", entry["node"], lang)
        if is_legacy_python(src):
            # Legacy Python nodes from pre-TypeScript tree versions.
            ns = {"__builtins__": dict(SAFE_BUILTINS), "json": json, "re": re, "math": math}
            exec(src, ns)
            ns["run"](ctx)
        else:
            from . import tscode
            # Pass real JSON values through (vars/result are JSON, n8n-style).
            new_ctx = tscode.run_code(src, {"input": ctx["input"],
                                            "vars": ctx["vars"],
                                            "result": ctx["result"]})
            ctx["vars"] = new_ctx.get("vars", ctx["vars"])
            ctx["result"] = new_ctx.get("result", ctx["result"])
        entry["vars_after"] = {k: logsetup.preview(v, 220) for k, v in ctx["vars"].items()}
        if ctx["result"] is not None:
            entry["result_preview"] = logsetup.preview(ctx["result"], 400)
        return node.get("next")


def _fill(template, ctx):
    """Substitute {input} / {vars.key}. JSON values are inserted as JSON text."""
    out = template.replace("{input}", _as_text(ctx["input"]))
    for k, v in ctx["vars"].items():
        out = out.replace("{vars.%s}" % k, _as_text(v))
    return out


def _as_text(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

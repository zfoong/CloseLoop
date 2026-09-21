"""Decision/action tree — the artifact S2 shapes and S1 executes (multi-port).

Envelope: ctx = { inputs: {name→json}, vars: {name→json}, outputs: {name→json} }.
Nodes read {inputs.<name>} / {vars.<key>} and fill named output ports.

Tree JSON:
{
  "entry": nodeId,
  "nodes": { id: {kind: jev|llm|code|tool, ...} },
  "evaluation": { <output_port_name>: {code?, s1_battery?, s1_pass_threshold?} },
  "task": { "kind": str, "sample_inputs"?: {...} }   # optional, for the generator
}

Node kinds:
  jev  {questions, gates?, route?, next?, on_error?}
  llm  {system?, prompt, output_key?, output_port?, next?, on_error?}
  code {source(TS run(ctx)), next?, on_error?}
  tool {tool, args?, output_key?, output_port?, next?, on_error?}

route.on may be a question key (branch on the Jev answer) OR a plain JSON path
"inputs.x.field" / "vars.k.field" (deterministic switch, no model call).
"""
import json
import re

from . import logsetup, schema

log = logsetup.get("tree")

MAX_STEPS = 40


class TreeError(Exception):
    pass


# ------------------------------------------------------------------ validation
def validate(tree):
    """Structural graph validation (before a tree version is accepted)."""
    if not isinstance(tree, dict):
        raise TreeError("tree must be an object")
    nodes = tree.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        raise TreeError("tree.nodes must be a non-empty object")
    if tree.get("entry") not in nodes:
        raise TreeError(f"tree.entry '{tree.get('entry')}' is not a node id")
    for nid, node in nodes.items():
        for e in schema.validate_node(nid, node):
            raise TreeError(e)
        if node["kind"] == "code":
            src = node.get("source", "")
            if "function run(" not in src and "def run(" not in src:
                raise TreeError(f"code node '{nid}' must define `function run(ctx: Ctx)`")
            if "import " in src or "require(" in src:
                raise TreeError(f"code node '{nid}': imports are not allowed")
        for ref in _refs(node):
            if ref is not None and ref not in nodes:
                raise TreeError(f"node '{nid}' references unknown node '{ref}'")
    # reachability
    reachable, frontier = {tree["entry"]}, [tree["entry"]]
    while frontier:
        for ref in _refs(nodes[frontier.pop()]):
            if ref is not None and ref not in reachable:
                reachable.add(ref); frontier.append(ref)
    orphans = set(nodes) - reachable
    if orphans:
        raise TreeError(f"unreachable nodes: {', '.join(sorted(orphans))}")
    return True


def _refs(node):
    yield node.get("next")
    for g in node.get("gates", []) or []:
        yield g.get("escalate_to")
    route = node.get("route") or {}
    yield route.get("default"); yield route.get("if_true"); yield route.get("if_false")
    for t in (route.get("branches") or {}).values():
        yield t
    oe = node.get("on_error")
    if isinstance(oe, dict):
        yield oe.get("escalate")


def declared_outputs(tree):
    """Output-port names the tree writes: explicit output_port + code `outputs[...]`."""
    produced = set()
    for node in tree.get("nodes", {}).values():
        if node.get("output_port"):
            produced.add(node["output_port"])
        if node.get("kind") == "code":
            for m in re.findall(r"outputs\s*\[\s*['\"]([^'\"]+)['\"]\s*\]", node.get("source", "")):
                produced.add(m)
            for m in re.findall(r"outputs\.([A-Za-z_]\w*)", node.get("source", "")):
                produced.add(m)
    return produced


def compile_tree(tree, contract=None):
    """Whole-graph compile gate: structural validation + output wiring + tsc.
    Returns a list of error strings ([] = clean)."""
    from . import tscode
    try:
        validate(tree)
    except TreeError as e:
        log.warning("compile: graph invalid: %s", e)
        return [f"graph: {e}"]
    errors = []
    # output-wiring check (I6): every required output port must be produced
    if contract:
        produced = declared_outputs(tree)
        for p in contract.get("outputs", []):
            if p.get("required", True) and p["name"] not in produced:
                errors.append(f"output port '{p['name']}' is never produced by any node "
                              f"(set output_port=\"{p['name']}\" on an llm/tool node, or write ctx.outputs[\"{p['name']}\"] in code)")
    # tsc on code nodes
    for nid, node in tree.get("nodes", {}).items():
        if node.get("kind") == "code":
            err = tscode.typecheck(node.get("source", ""), "run")
            if err:
                errors.append(f"code node '{nid}': {err}")
    # tsc on per-output evaluation code
    for port, spec in (tree.get("evaluation") or {}).items():
        if spec.get("code"):
            err = tscode.typecheck(spec["code"], "eval")
            if err:
                errors.append(f"evaluation[{port}] code: {err}")
    if errors:
        log.warning("compile: %d error(s): %s", len(errors), logsetup.preview("; ".join(errors), 300))
    else:
        log.debug("compile: clean")
    return errors


# ------------------------------------------------------------------ runtime
class TreeRunner:
    """Walks a tree for one input bundle. Code owns control flow (R-2.4)."""

    def __init__(self, tree, jev_client, llm_client):
        self.tree = tree
        self.jev = jev_client
        self.llm = llm_client

    def run(self, inputs):
        ctx = {"inputs": inputs, "vars": {}, "outputs": {}}
        trace = []
        node_id = self.tree["entry"]
        steps, escalated = 0, False
        log.debug("tree run start: entry=%s | inputs=%s", node_id, logsetup.preview(inputs, 160))

        while node_id is not None and steps < MAX_STEPS:
            steps += 1
            node = self.tree["nodes"][node_id]
            entry = {"node": node_id, "kind": node["kind"]}
            try:
                if node["kind"] == "jev":
                    node_id, esc = self._run_jev(node, ctx, entry)
                    escalated = escalated or esc
                elif node["kind"] == "llm":
                    node_id = self._run_llm(node, ctx, entry)
                elif node["kind"] == "code":
                    node_id = self._run_code(node, ctx, entry)
                elif node["kind"] == "tool":
                    node_id = self._run_tool(node, ctx, entry)
            except Exception as e:
                entry["error"] = str(e)
                node_id = self._on_error(node, entry, e)
                trace.append(entry)
                if node_id is None and entry.get("aborted"):
                    log.error("node '%s' (%s) failed, run aborted: %s", entry["node"], entry["kind"], e)
                    return {"outputs": ctx["outputs"], "trace": trace, "error": str(e), "escalated": escalated}
                continue
            trace.append(entry)

        if steps >= MAX_STEPS and node_id is not None:
            log.warning("tree run hit MAX_STEPS=%d — stopping", MAX_STEPS)
        log.debug("tree run end: %d steps | escalated=%s | outputs=%s",
                  steps, escalated, logsetup.preview(ctx["outputs"], 160))
        return {"outputs": ctx["outputs"], "trace": trace, "error": None, "escalated": escalated}

    def _on_error(self, node, entry, exc):
        policy = node.get("on_error", "abort")
        if isinstance(policy, dict) and policy.get("escalate"):
            entry["on_error"] = "escalate:" + policy["escalate"]
            return policy["escalate"]
        if policy == "skip":
            entry["on_error"] = "skip"
            return node.get("next")
        entry["aborted"] = True
        return None

    # -- S1 decision node ----------------------------------------------------
    def _run_jev(self, node, ctx, entry):
        state = {"inputs": ctx["inputs"], "vars": ctx["vars"]}
        answers, meta = self.jev.system_one(state, node["questions"])
        entry["answers"] = answers
        entry["meta"] = meta

        for gate in node.get("gates", []) or []:
            ans = answers.get(gate["question"], {})
            conf = ans.get("confidence")
            if conf is None and ans.get("type") == "noul":
                conf = abs(ans["noul"] - 0.5) * 2
            if conf is not None and conf < gate["min_confidence"]:
                entry["gate_fired"] = {"question": gate["question"], "confidence": conf}
                log.warning("gate fired at '%s': %s conf %.2f < %.2f → %s",
                            entry["node"], gate["question"], conf, gate["min_confidence"], gate["escalate_to"])
                return gate["escalate_to"], True

        route = node.get("route")
        if route:
            on = route["on"]
            if on in answers:  # branch on a Jev answer
                ans = answers[on]
                if ans.get("type") == "choice":
                    tgt = (route.get("branches") or {}).get(ans["choice"], route.get("default"))
                elif ans.get("type") == "noul":
                    tgt = route.get("if_true") if ans["noul"] >= route.get("noul_threshold", 0.5) else route.get("if_false")
                elif ans.get("type") == "score":
                    tgt = (route.get("branches") or {}).get(str(int(ans["score"])), route.get("default"))
                else:
                    tgt = route.get("default")
            else:  # deterministic field-based routing (no model call)
                val = _resolve_path(ctx, on)
                tgt = (route.get("branches") or {}).get(str(val), route.get("default"))
            entry["routed_to"] = tgt
            return tgt, False
        return node.get("next"), False

    # -- S2 generation node --------------------------------------------------
    def _run_llm(self, node, ctx, entry):
        prompt = _fill(node["prompt"], ctx)
        system = node.get("system", "You are a helpful assistant.") + \
            "\nRespond with a single JSON object (no prose outside the JSON)."
        text, meta = self.llm.complete(system, prompt, force_json=True)
        obj = _parse_json_obj(text)
        key = node.get("output_key", "output")
        ctx["vars"][key] = obj
        if node.get("output_port"):
            ctx["outputs"][node["output_port"]] = obj
            entry["output_port"] = node["output_port"]
        entry["prompt_preview"] = prompt[:400]
        entry["output_key"] = key
        entry["output_preview"] = json.dumps(obj)[:400]
        entry["meta"] = meta
        return node.get("next")

    # -- code node -----------------------------------------------------------
    def _run_code(self, node, ctx, entry):
        from . import tscode
        entry["vars_before"] = {k: logsetup.preview(v, 200) for k, v in ctx["vars"].items()}
        new_ctx = tscode.run_code(node["source"], {"inputs": ctx["inputs"], "vars": ctx["vars"], "outputs": ctx["outputs"]})
        ctx["vars"] = new_ctx.get("vars", ctx["vars"])
        ctx["outputs"] = new_ctx.get("outputs", ctx["outputs"])
        entry["vars_after"] = {k: logsetup.preview(v, 200) for k, v in ctx["vars"].items()}
        entry["outputs_after"] = {k: logsetup.preview(v, 200) for k, v in ctx["outputs"].items()}
        return node.get("next")

    # -- tool node -----------------------------------------------------------
    def _run_tool(self, node, ctx, entry):
        from . import tools
        args = _resolve_args(node.get("args", {}), ctx)
        result = tools.run(node["tool"], args)
        key = node.get("output_key", node["tool"] + "_out")
        ctx["vars"][key] = result
        if node.get("output_port"):
            ctx["outputs"][node["output_port"]] = result
            entry["output_port"] = node["output_port"]
        entry["tool"] = node["tool"]
        entry["output_preview"] = logsetup.preview(result, 300)
        return node.get("next")


# ------------------------------------------------------------------ helpers
def _parse_json_obj(text):
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"value": obj}
    except (ValueError, TypeError):
        return {"text": text}


def _fill(template, ctx):
    """Substitute {inputs.<name>} / {vars.<key>} / {outputs.<port>} with JSON text.
    Output ports already filled upstream are readable so a later node can assemble
    them (e.g. render a document from earlier outputs)."""
    def sub(m):
        return _as_text(_resolve_path(ctx, m.group(1)))
    return re.sub(r"\{((?:inputs|vars|outputs)\.[^}]+)\}", sub, template)


def _resolve_args(args, ctx):
    """Resolve {inputs.x}/{vars.y} inside tool args. A whole-string placeholder
    yields the raw JSON value (so an asset ref passes through intact)."""
    if isinstance(args, str):
        m = re.fullmatch(r"\{((?:inputs|vars|outputs)\.[^}]+)\}", args.strip())
        if m:
            return _resolve_path(ctx, m.group(1))
        return _fill(args, ctx)
    if isinstance(args, dict):
        return {k: _resolve_args(v, ctx) for k, v in args.items()}
    if isinstance(args, list):
        return [_resolve_args(v, ctx) for v in args]
    return args


def _resolve_path(ctx, path):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _as_text(v):
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

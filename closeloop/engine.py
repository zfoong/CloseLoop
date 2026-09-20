"""The CloseLoop engine: one loop = run task -> evaluate -> (maybe) shape.

All operations are task-scoped (multiple tasks, each with its own tree
versions and run history). Humans define a task in plain language (kind,
expected output, metric) and S2 bootstraps the initial decision/action tree
(REQUIREMENT.md open question 4: minimal template fallback when S2 is
unavailable).
"""
import json
import re
import time

from . import logsetup, store, tree as tree_mod, tscode

log = logsetup.get("engine")

TREE_SCHEMA_HELP = """DATA MODEL (n8n-style: everything is JSON). Nodes share a context
ctx = {input, vars, result}: `input` is the task input, `vars` is a JSON object of named values
produced by nodes, `result` is the final JSON output. Every value passed between nodes is JSON.

Tree JSON schema: {"task": {"kind": str, "sample_inputs": [str,...]},
"entry": id, "nodes": {id: {...}}, "evaluation": {...}}.
Node kinds:
- jev (System 1 decision): {"kind":"jev","questions":{key:{type:"noul"|"choice"|"score","instructions":...,
  "criteria":...}}, "gates":[{"question":key,"min_confidence":0..1,"escalate_to":id}],
  "route":{"on":key,"branches":{option:id},"default":id} or {"on":key,"noul_threshold":0.5,"if_true":id,"if_false":id},
  "next":id}. choice criteria = {option: description}; score criteria = [level descriptions]; noul criteria optional {"true":...,"false":...}.
- llm (System 2 generation): {"kind":"llm","system":str,"prompt":str with {input} and {vars.key} placeholders,
  "output_key":str,"next":id}. The llm ALWAYS returns a JSON OBJECT, stored as vars[output_key]. Tell it in the
  prompt exactly which JSON fields to produce (e.g. {"reply": "..."} or {"tag": "...", "reason": "..."}).
  A {vars.key} placeholder is substituted with that value's JSON text. A terminal node's output is the result.
- code (executable): {"kind":"code","source":"<TypeScript>","next":id}. Source MUST be TypeScript defining
  `function run(ctx: Ctx): void` where `type Ctx = { input: Json; vars: Record<string,Json>; result: Json }`
  and Json is any JSON value (Json, Verdict, Ctx are predeclared — do NOT redeclare them). Read JSON from
  ctx.vars (e.g. narrow with typeof/`as`), mutate ctx, set ctx.result (any JSON) for the final output.
  NO import/require — plain TypeScript with standard JS built-ins only. Every code node is compiled with
  `tsc --strict` before the tree is accepted: a type error anywhere rejects the whole edit.
"evaluation": {"code": "<TypeScript defining `function evaluate(taskInput: Json, result: Json): Verdict`
where `type Verdict = { passed: boolean; score: number; notes: string }` — same rules as code nodes>",
"s1_battery": {key: noul/choice/score question over state {task_input, result}}, "s1_pass_threshold": 0..1}."""

SHAPER_SYSTEM = """You are the SHAPER (System 2) of a CloseLoop self-improving system.
You will receive the current decision/action tree (JSON) and recent run records: for each run you see
the input, the ACTUAL result the tree produced, a trace of what each node decided/produced, and the
evaluation breakdown (each check's score and whether it passed). Read the actual results and the
failing checks before deciding — diagnose from what really happened, not from guesses.
Your job: decide whether the tree needs to change. If results are good and stable, DO NOT change it.
If there are failures, edit the tree: you may rewrite Jev questions/criteria/thresholds,
add/remove/reorder nodes, rewrite code node sources, rewrite llm node prompts, or adjust the
evaluation battery/code. Edits must generalize across the input distribution — never overfit to one input.

Know what each tool can do, so you fix the right thing:
- jev nodes JUDGE meaning (classify/score/route). They cannot generate text or compute.
- llm nodes are the ONLY nodes that can GENERATE natural language or novel content.
- code nodes are deterministic glue; they cannot judge meaning or write prose.
So if the result reads poorly or a meaning/quality check keeps failing, the fix is usually a better
llm node (or its prompt), NOT more code. If routing is wrong, fix the jev question/criteria.
""" + TREE_SCHEMA_HELP + """
Respond with STRICT JSON only:
  {"shape": false, "reason": "..."}  OR
  {"shape": true, "reason": "...", "tree": { ...complete new tree... }}"""

BOOTSTRAP_SYSTEM = """You are the ARCHITECT (System 2) of a CloseLoop self-improving system.
A user described a task in plain language. Design the INITIAL decision/action tree for it.
Guidelines:
- Keep it simple: typically an entry jev node (triage/routing or applicability checks), one llm node
  that produces the output as a JSON object, a jev validation node checking the output (route failures
  to a stronger llm fallback node), and a TypeScript code finalize node that shapes ctx.result.
- llm nodes always return a JSON object — design their fields to match the required output.
- Jev nodes CANNOT generate text and CANNOT do math/date comparisons; use them only for judgement.
- Write a meaningful evaluation: a code metric (cheap structural checks) plus an s1_battery of
  noul questions judging the result against the task description.
- task.sample_inputs: include the user's examples if given; add realistic varied ones until there
  are at least 8. They must be genuinely different situations.
""" + TREE_SCHEMA_HELP + """
Respond with STRICT JSON only: {"tree": { ...complete tree... }}"""

GENERATOR_SYSTEM = """You generate task inputs for a CloseLoop system.
Given a task kind and example inputs, produce NEW, realistic, varied task instances of the SAME KIND.
They must be meaningfully different from the examples and from each other (different situations,
details, tones — not paraphrases). Respond with STRICT JSON only: {"inputs": ["...", "..."]}"""


class Engine:
    def __init__(self, jev_client, llm_client):
        self.jev = jev_client
        self.llm = llm_client

    # ------------------------------------------------------ task creation
    def create_task(self, name, description, output_desc, metric_desc, examples):
        """Bootstrap a new task from a plain-language definition. Returns (task_id, how)."""
        form = {
            "task_description": description,
            "expected_output": output_desc,
            "how_to_judge": metric_desc or "Judge whether the result fulfils the task description and matches the expected output.",
            "example_inputs": examples,
        }
        log.info("create_task '%s': %s | %d example(s)", name, logsetup.preview(description, 120), len(examples))
        tree, how = None, "template"
        if not self.llm.mock:
            try:
                text, _meta = self.llm.complete(BOOTSTRAP_SYSTEM, json.dumps(form, indent=1), force_json=True)
                candidate = json.loads(text).get("tree")
                compile_errors = tree_mod.compile_tree(candidate)  # graph + tsc gate
                if compile_errors:
                    raise ValueError("bootstrap tree failed compilation: " + "; ".join(compile_errors))
                if not candidate.get("task", {}).get("sample_inputs"):
                    raise ValueError("bootstrap tree has no sample_inputs")
                tree, how = candidate, "s2"
                log.info("architect (S2) designed tree: %d nodes, %d sample inputs",
                         len(candidate.get("nodes", {})), len(candidate["task"]["sample_inputs"]))
            except Exception as e:
                log.warning("architect (S2) bootstrap failed (%s) — using template tree", logsetup.preview(str(e), 160))
                tree = None
        if tree is None:
            tree = _template_tree(description, output_desc, form["how_to_judge"], examples)
        task_id = store.create_task(name, {"description": description, "expected_output": output_desc,
                                           "how_to_judge": form["how_to_judge"]})
        store.save_tree(task_id, tree, f"bootstrap ({'S2-designed' if how == 's2' else 'template'})")
        log.info("task created: id='%s' bootstrapped_by=%s", task_id, how)
        return task_id, how

    # ---------------------------------------------------------------- run
    def run_once(self, task_id, task_input, shape_after=True):
        tree, version = store.load_tree(task_id)
        log.info("loop start: task='%s' tree=%s | input: %s",
                 task_id, version, logsetup.preview(task_input, 140))
        runner = tree_mod.TreeRunner(tree, self.jev, self.llm)
        started = time.time()
        outcome = runner.run(task_input)
        evaluation = self.evaluate(tree, task_input, outcome["result"])
        duration = int((time.time() - started) * 1000)

        path = " -> ".join(t["node"] for t in outcome["trace"])
        level = log.info if evaluation["passed"] else log.warning
        level("loop end: task='%s' tree=%s | %s | escalated=%s | %dms | path: %s",
              task_id, version, "PASS" if evaluation["passed"] else "FAIL",
              outcome["escalated"], duration, path)
        if outcome["error"]:
            log.error("loop error: task='%s' | %s", task_id, outcome["error"])

        record = {
            "ts": int(started),
            "tree_version": version,
            "input": task_input,
            "result": outcome["result"],
            "error": outcome["error"],
            "escalated": outcome["escalated"],
            "trace": outcome["trace"],
            "evaluation": evaluation,
            "duration_ms": duration,
        }

        if shape_after:
            record["shaping"] = self.shape(task_id)

        store.append_run(task_id, record)
        return record

    # --------------------------------------------------------------- eval
    def evaluate(self, tree, task_input, result):
        ev = tree.get("evaluation", {})
        report = {"passed": True, "parts": {}}

        # (a/d) code metric — S2-authored validator stored in the tree
        code = ev.get("code")
        if code and result is not None:
            try:
                if tree_mod.is_legacy_python(code):
                    ns = {"__builtins__": dict(tree_mod.SAFE_BUILTINS),
                          "json": json, "re": re, "math": tree_mod.math}
                    exec(code, ns)
                    verdict = ns["evaluate"](task_input, result)
                else:
                    verdict = tscode.run_evaluate(code, str(task_input), str(result))
                report["parts"]["code_metric"] = verdict
                report["passed"] = report["passed"] and bool(verdict.get("passed", False))
                log.debug("eval code_metric: passed=%s notes=%s",
                          verdict.get("passed"), logsetup.preview(verdict.get("notes", ""), 120))
            except Exception as e:
                report["parts"]["code_metric"] = {"passed": False, "notes": f"validator crashed: {e}"}
                report["passed"] = False
                log.warning("eval code_metric crashed: %s", logsetup.preview(str(e), 160))
        elif result is None:
            report["parts"]["code_metric"] = {"passed": False, "notes": "no result produced"}
            report["passed"] = False
            log.warning("eval: no result produced by the tree")

        # (b) S1 validation battery — one batched Jev call
        battery = ev.get("s1_battery")
        if battery and result is not None:
            threshold = ev.get("s1_pass_threshold", 0.6)
            try:
                answers, meta = self.jev.system_one(
                    {"task_input": task_input, "result": result}, battery)
                checks = {}
                ok = True
                for key, ans in answers.items():
                    if ans.get("type") == "noul":
                        p = ans["noul"]
                        checks[key] = {"noul": p, "passed": p >= threshold}
                        ok = ok and p >= threshold
                    else:
                        checks[key] = ans
                report["parts"]["s1_battery"] = {"checks": checks, "threshold": threshold,
                                                 "passed": ok, "mock": meta.get("mock", False)}
                report["passed"] = report["passed"] and ok
                failed = [k for k, v in checks.items() if isinstance(v, dict) and v.get("passed") is False]
                log.debug("eval s1_battery: passed=%s thr=%.2f failed=%s", ok, threshold, failed or "none")
            except Exception as e:
                report["parts"]["s1_battery"] = {"passed": False, "notes": f"S1 battery failed: {e}"}
                report["passed"] = False
                log.warning("eval s1_battery failed: %s", logsetup.preview(str(e), 160))

        return report

    # -------------------------------------------------------------- shape
    def shape(self, task_id):
        """S2 inspects recent runs and edits the tree — or declines (R-5.1).
        The shaper sees the ACTUAL result, node trace, and per-check scores for
        each run (failures first) — it diagnoses from evidence, not guesses."""
        tree, version = store.load_tree(task_id)
        runs = store.load_runs(task_id, limit=12)
        # Depth over breadth: show a few runs IN FULL, failures first for signal.
        fails = [r for r in runs if not r["evaluation"]["passed"]]
        passes = [r for r in runs if r["evaluation"]["passed"]]
        chosen = (fails[-4:] + passes[-2:]) or runs[-4:]
        chosen.sort(key=lambda r: r["ts"])
        digest = [_run_evidence(r) for r in chosen]
        log.debug("shape: task='%s' tree=%s | evidence from %d run(s) (%d fail / %d pass in window)",
                  task_id, version, len(chosen), len(fails), len(passes))

        user = ("CURRENT TREE (version %s):\n%s\n\nRECENT RUNS (newest last) — read the results and "
                "failing checks:\n%s"
                % (version, json.dumps(_without_meta(tree), indent=1),
                   json.dumps(digest, indent=1)))
        try:
            text, meta = self.llm.complete(SHAPER_SYSTEM, user, force_json=True)
        except Exception as e:
            # Shaping is best-effort: an unavailable S2 must never kill the loop.
            log.error("shape: shaper unavailable for task='%s': %s", task_id, e)
            return {"shaped": False, "reason": f"shaper unavailable: {e}", "mock": None}

        try:
            decision = json.loads(text)
        except json.JSONDecodeError:
            log.warning("shape: task='%s' shaper returned non-JSON — edit rejected", task_id)
            return {"shaped": False, "reason": "shaper returned non-JSON; edit rejected", "mock": meta.get("mock")}

        if not decision.get("shape"):
            log.info("shape: task='%s' DECLINED — %s", task_id, logsetup.preview(decision.get("reason", ""), 200))
            return {"shaped": False, "reason": decision.get("reason", ""), "mock": meta.get("mock")}

        new_tree = decision.get("tree")
        # Compile gate (R-5.5): whole-graph validation + tsc type-check of every
        # code node. A tree that does not compile is never deployed.
        compile_errors = tree_mod.compile_tree(new_tree)
        if compile_errors:
            log.warning("shape: task='%s' edit REJECTED by compile gate: %s",
                        task_id, logsetup.preview(" | ".join(compile_errors), 300))
            return {"shaped": False,
                    "reason": "edit rejected by compile gate: " + " | ".join(compile_errors)[:500],
                    "mock": meta.get("mock")}

        if "task" not in new_tree and "task" in tree:
            new_tree["task"] = tree["task"]  # shaper must not lose the task definition
        new_version = store.save_tree(task_id, new_tree, decision.get("reason", "shaper edit"))
        log.info("shape: task='%s' EDITED %s -> %s | %d nodes | reason: %s",
                 task_id, version, new_version, len(new_tree.get("nodes", {})),
                 logsetup.preview(decision.get("reason", ""), 200))
        return {"shaped": True, "reason": decision.get("reason", ""),
                "new_version": new_version, "mock": meta.get("mock")}

    # --------------------------------------------------------- task stream
    def new_inputs(self, task_id, n):
        """Produce n NEW task instances of the same kind (R-3.0): every loop
        gets a different input. Live S2 synthesizes them; otherwise the
        sample pool is cycled with detail variation."""
        tree, _ = store.load_tree(task_id)
        task = tree.get("task", {})
        pool = task.get("sample_inputs", [])
        counter = store.run_count(task_id)

        if not self.llm.mock:
            try:
                user = json.dumps({
                    "task_kind": task.get("kind", "unknown task"),
                    "example_inputs": pool[:6],
                    "count": n,
                })
                text, _meta = self.llm.complete(GENERATOR_SYSTEM, user, force_json=True)
                inputs = [s.strip() for s in json.loads(text).get("inputs", [])
                          if isinstance(s, str) and s.strip()]
                if len(inputs) >= n:
                    log.info("new_inputs: task='%s' generated %d instance(s) via S2", task_id, n)
                    return inputs[:n]
                log.warning("new_inputs: S2 returned %d < %d — falling back to local variation", len(inputs), n)
            except Exception as e:
                log.warning("new_inputs: S2 generation failed (%s) — local variation", logsetup.preview(str(e), 120))

        out = []
        for i in range(n):
            base = pool[(counter + i) % len(pool)] if pool else "Sample task instance"
            out.append(_vary(base, counter + i))
        log.info("new_inputs: task='%s' produced %d instance(s) via local variation", task_id, n)
        return out

    # ------------------------------------------------------------ metrics
    @staticmethod
    def metrics(task_id):
        """Convergence indicators over the run history (R-6.3)."""
        runs = store.load_runs(task_id)
        if not runs:
            return {"runs": 0, "pass_rate": None, "escalation_rate": None,
                    "shaping_edits": 0, "tree_version": store.latest_version(task_id)}
        window = runs[-20:]
        shaped = sum(1 for r in runs if (r.get("shaping") or {}).get("shaped"))
        return {
            "runs": len(runs),
            "pass_rate": round(sum(1 for r in window if r["evaluation"]["passed"]) / len(window), 2),
            "escalation_rate": round(sum(1 for r in window if r["escalated"]) / len(window), 2),
            "shaping_edits": shaped,
            "tree_version": store.latest_version(task_id),
        }


# ------------------------------------------------------------------ helpers
def _template_tree(description, output_desc, metric_desc, examples):
    """Generic bootstrap tree used when S2 is unavailable: produce -> validate
    -> finalize, with a stronger-S2 fallback path."""
    samples = examples or [f"Example input {i + 1} for: {description[:60]}" for i in range(4)]
    return {
        "task": {"kind": description, "sample_inputs": samples},
        "entry": "produce",
        "nodes": {
            "produce": {
                "kind": "llm",
                "system": f"You perform this task: {description}\nRequired output: {output_desc}\nReturn a JSON object {{\"output\": <the required output as a string>}}.",
                "prompt": "Task input:\n{input}\n\nProduce the required output as JSON {\"output\": \"...\"}.",
                "output_key": "draft",
                "next": "validate",
            },
            "validate": {
                "kind": "jev",
                "questions": {
                    "fulfils_task": {
                        "type": "noul",
                        "instructions": {
                            "task": description,
                            "expected_output": output_desc,
                            "main_question": "Does the `draft` fulfil the task for `task_input`?",
                        },
                    },
                },
                "route": {"on": "fulfils_task", "noul_threshold": 0.5,
                          "if_true": "finalize", "if_false": "retry"},
            },
            "retry": {
                "kind": "llm",
                "system": f"You are a senior specialist (System 2). Task: {description}\nRequired output: {output_desc}\nThe first attempt was judged insufficient. Do it carefully. Return JSON {{\"output\": \"...\"}}.",
                "prompt": "Task input:\n{input}\n\nFirst attempt (judged insufficient):\n{vars.draft}\n\nProduce a better output as JSON {\"output\": \"...\"}.",
                "output_key": "draft",
                "next": "finalize",
            },
            "finalize": {
                "kind": "code",
                "source": ("function run(ctx: Ctx): void {\n"
                           "  const draft = ctx.vars['draft'];\n"
                           "  const output = (draft && typeof draft === 'object' && !Array.isArray(draft))\n"
                           "    ? (draft as { [k: string]: Json })['output'] : draft;\n"
                           "  ctx.result = { output: typeof output === 'string' ? output.trim() : output };\n"
                           "}\n"),
                "next": None,
            },
        },
        "evaluation": {
            "code": ("function evaluate(taskInput: Json, result: Json): Verdict {\n"
                     "  const out = (result && typeof result === 'object' && !Array.isArray(result))\n"
                     "    ? (result as { [k: string]: Json })['output'] : result;\n"
                     "  const s = typeof out === 'string' ? out : JSON.stringify(out ?? '');\n"
                     "  const ok = s.trim().length >= 10;\n"
                     "  return { passed: ok, score: ok ? 1 : 0, notes: ok ? 'result present' : 'result missing or too short' };\n}\n"),
            "s1_battery": {
                "fulfils_task": {
                    "type": "noul",
                    "instructions": {
                        "task": description,
                        "expected_output": output_desc,
                        "how_to_judge": metric_desc,
                        "main_question": "Does the `result` fulfil the task for `task_input`?",
                    },
                },
            },
            "s1_pass_threshold": 0.6,
        },
    }


def _without_meta(tree):
    return {k: v for k, v in tree.items() if k != "_meta"}


def _run_evidence(r):
    """Full evidence for one run: input, ACTUAL result, node trace, and the
    per-check evaluation scores. This is what lets the shaper diagnose instead
    of guess (it sees the output it is improving and exactly what failed)."""
    trace = []
    for t in r.get("trace", []):
        step = {"node": t["node"], "kind": t["kind"]}
        if t.get("answers"):  # jev: show decisions + probabilities/confidence
            step["decided"] = {k: (a.get("choice") or a.get("noul") or a.get("score"))
                               for k, a in t["answers"].items()}
            conf = {k: a["confidence"] for k, a in t["answers"].items() if "confidence" in a}
            if conf:
                step["confidence"] = conf
        if t.get("routed_to"):
            step["routed_to"] = t["routed_to"]
        if t.get("gate_fired"):
            step["gate_fired"] = t["gate_fired"]
        if t.get("output_preview"):  # llm: what it generated
            step["produced"] = t["output_preview"]
        if t.get("error"):
            step["error"] = t["error"]
        trace.append(step)

    # Evaluation broken out per check, with scores — not just a boolean.
    parts = r["evaluation"].get("parts", {})
    checks = {}
    cm = parts.get("code_metric")
    if isinstance(cm, dict):
        checks["code_metric"] = {"passed": cm.get("passed"), "notes": cm.get("notes")}
    s1 = parts.get("s1_battery")
    if isinstance(s1, dict):
        checks["s1_battery"] = {k: v for k, v in s1.get("checks", {}).items()}

    return {
        "input": str(r["input"])[:400],
        "result": (str(r["result"])[:500] if r["result"] is not None else None),
        "passed": r["evaluation"]["passed"],
        "error": r["error"],
        "trace": trace,
        "checks": checks,
    }


def _vary(text, k):
    """Local-mode variation: change every number so cycled samples still
    differ per loop; append a reference code if there are no numbers."""
    def repl(m):
        return str((int(m.group(0)) * 7 + k * 131) % 9000 + 1000)
    varied = re.sub(r"\d+", repl, text)
    if varied == text:
        varied = f"{text} (ref CL-{1000 + (k * 37) % 9000})"
    return varied

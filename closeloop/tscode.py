"""TypeScript execution and compilation for S2-authored code (multi-port).

Code nodes and evaluators are TypeScript:
  - executed with Node's native type stripping (Node >= 23),
  - type-checked with `tsc --noEmit --strict` before a tree is accepted.

Contracts (Ctx/Verdict/Json predeclared — do NOT redeclare):
  code node   :  function run(ctx: Ctx): void            (reads inputs/vars, writes vars/outputs)
  evaluator   :  function evaluate(output: Json, inputs: Record<string, Json>): Verdict
  code_exec   :  function run(input: Json): Json          (tool snippet)

No import/require in source.
"""
import json
import os
import re
import subprocess
import tempfile

from . import logsetup
from .config import ROOT

log = logsetup.get("tscode")

NODE = "node"
TSC = os.path.join(ROOT, "node_modules", ".bin", "tsc.cmd" if os.name == "nt" else "tsc")

TS_PRELUDE = """type Json = string | number | boolean | null | Json[] | { [k: string]: Json };
type Ctx = { inputs: Record<string, Json>; vars: Record<string, Json>; outputs: Record<string, Json> };
type Verdict = { passed: boolean; score?: number; notes?: string };
declare const require: any;
declare const process: any;
"""

_RUN_WRAPPER = """
const __ctx: Ctx = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
run(__ctx);
process.stdout.write('\\n__CTX__' + JSON.stringify(__ctx));
"""

_EVAL_WRAPPER = """
const __in = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const __v: Verdict = evaluate(__in.output, __in.inputs);
process.stdout.write('\\n__CTX__' + JSON.stringify(__v));
"""

_SNIPPET_WRAPPER = """
const __in = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const __r: Json = run(__in.input);
process.stdout.write('\\n__CTX__' + JSON.stringify(__r ?? null));
"""

_WRAPPERS = {"run": _RUN_WRAPPER, "eval": _EVAL_WRAPPER, "snippet": _SNIPPET_WRAPPER}


_REDECL = re.compile(r"^\s*(type\s+(Json|Ctx|Verdict)\b|declare\s+const\s+(require|process)\b).*$", re.M)


def _strip_redecls(source):
    """Remove any redeclaration of the predeclared Json/Ctx/Verdict types (a
    common S2 mistake that would trigger a tsc 'Duplicate identifier' error)."""
    return _REDECL.sub("", source or "")


def _write_ts(source, wrapper, dirpath):
    path = os.path.join(dirpath, "node.ts")
    with open(path, "w", encoding="utf-8") as f:
        f.write(TS_PRELUDE + _strip_redecls(source) + "\n" + wrapper)
    return path


def _execute(source, wrapper, payload, timeout=20):
    with tempfile.TemporaryDirectory() as d:
        path = _write_ts(source, wrapper, d)
        proc = subprocess.run([NODE, path], input=json.dumps(payload), capture_output=True,
                              text=True, encoding="utf-8", timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:400]
        log.error("TS execution failed (exit %s): %s", proc.returncode, detail)
        raise RuntimeError(f"TS node failed: {detail}")
    out = proc.stdout
    marker = out.rfind("__CTX__")
    if marker < 0:
        log.error("TS execution produced no __CTX__ payload; stdout: %s", logsetup.preview(out, 200))
        raise RuntimeError("TS node produced no __CTX__ payload")
    return json.loads(out[marker + len("__CTX__"):])


def run_code(source, ctx, timeout=20):
    """Execute a code node: run(ctx) mutates ctx; returns the new ctx."""
    return _execute(source, _RUN_WRAPPER, ctx, timeout)


def run_evaluate(source, output, inputs, timeout=20):
    """Execute an evaluator: evaluate(output, inputs) -> {passed, score, notes}."""
    return _execute(source, _EVAL_WRAPPER, {"output": output, "inputs": inputs}, timeout)


def run_snippet(source, input_value, timeout=20):
    """Execute a code_exec tool snippet: run(input) -> Json."""
    return _execute(source, _SNIPPET_WRAPPER, {"input": input_value}, timeout)


def typecheck(source, wrapper_kind="run"):
    """Compile-check one snippet with tsc --strict. Returns None or an error string."""
    wrapper = _WRAPPERS.get(wrapper_kind, _RUN_WRAPPER)
    if not os.path.exists(TSC):
        log.warning("tsc not found at %s — skipping type check (runtime type stripping still applies)", TSC)
        return None
    with tempfile.TemporaryDirectory() as d:
        path = _write_ts(source, wrapper, d)
        proc = subprocess.run(
            [TSC, "--noEmit", "--strict", "--target", "es2022", "--module", "commonjs", "--skipLibCheck", path],
            capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=ROOT)
    if proc.returncode != 0:
        msg = (proc.stdout or proc.stderr).strip()
        cleaned = [line.split("node.ts")[-1] for line in msg.splitlines() if line.strip()]
        result = ("\n".join(cleaned)[:600]) or "type check failed"
        log.debug("tsc type-check (%s) failed: %s", wrapper_kind, logsetup.preview(result, 200))
        return result
    log.debug("tsc type-check (%s) passed", wrapper_kind)
    return None

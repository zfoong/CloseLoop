"""TypeScript execution and compilation for S2-authored code.

Code nodes and evaluation validators are written in TypeScript:
  - executed with Node's native type stripping (Node >= 23),
  - type-checked with `tsc --noEmit --strict` before a tree version is
    accepted (the compile gate), so type errors are caught at shaping/
    bootstrap time, never at runtime.

Contracts:
  code node :  function run(ctx: Ctx): void        (mutates ctx)
  evaluator :  function evaluate(taskInput: string, result: string): Verdict

No imports are allowed in node source (the wrapper provides everything);
this is enforced by tree.validate().
"""
import json
import os
import subprocess
import tempfile

from . import logsetup
from .config import ROOT

log = logsetup.get("tscode")

NODE = "node"
TSC = os.path.join(ROOT, "node_modules", ".bin", "tsc.cmd" if os.name == "nt" else "tsc")

TS_PRELUDE = """type Json = string | number | boolean | null | Json[] | { [k: string]: Json };
type Ctx = { input: Json; vars: Record<string, Json>; result: Json };
type Verdict = { passed: boolean; score: number; notes: string };
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
const __v: Verdict = evaluate(__in.task_input, __in.result);
process.stdout.write('\\n__CTX__' + JSON.stringify(__v));
"""


def _write_ts(source, wrapper, dirpath):
    path = os.path.join(dirpath, "node.ts")
    with open(path, "w", encoding="utf-8") as f:
        f.write(TS_PRELUDE + source + "\n" + wrapper)
    return path


def _execute(source, wrapper, payload, timeout=15):
    with tempfile.TemporaryDirectory() as d:
        path = _write_ts(source, wrapper, d)
        proc = subprocess.run(
            [NODE, path], input=json.dumps(payload), capture_output=True,
            text=True, encoding="utf-8", timeout=timeout,
        )
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


def run_code(source, ctx, timeout=15):
    """Execute a TS code node: run(ctx) mutates ctx; returns the new ctx."""
    return _execute(source, _RUN_WRAPPER, ctx, timeout)


def run_evaluate(source, task_input, result, timeout=15):
    """Execute a TS evaluator: returns {passed, score, notes}."""
    return _execute(source, _EVAL_WRAPPER, {"task_input": task_input, "result": result}, timeout)


def typecheck(source, wrapper_kind="run"):
    """Compile-check one snippet with tsc --strict. Returns None or an error string."""
    wrapper = _RUN_WRAPPER if wrapper_kind == "run" else _EVAL_WRAPPER
    if not os.path.exists(TSC):
        log.warning("tsc not found at %s — skipping type check (runtime type stripping still applies)", TSC)
        return None  # tsc unavailable: runtime type stripping still applies
    with tempfile.TemporaryDirectory() as d:
        path = _write_ts(source, wrapper, d)
        proc = subprocess.run(
            [TSC, "--noEmit", "--strict", "--target", "es2022",
             "--module", "commonjs", "--skipLibCheck", path],
            capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=ROOT,
        )
    if proc.returncode != 0:
        msg = (proc.stdout or proc.stderr).strip()
        # keep diagnostics, drop the temp-file path prefix
        cleaned = [line.split("node.ts")[-1] for line in msg.splitlines() if line.strip()]
        result = ("\n".join(cleaned)[:600]) or "type check failed"
        log.debug("tsc type-check (%s) failed: %s", wrapper_kind, logsetup.preview(result, 200))
        return result
    log.debug("tsc type-check (%s) passed", wrapper_kind)
    return None

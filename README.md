# CloseLoop

A prototype of a **self-improving decision system** that loops between:

- **System 1 (S1)** — [TypeSafe Jev](https://docs.typesafe.ai), a fast/cheap decision model (Noul / Choice / Score primitives, calibrated probabilities) — the main runtime decision maker.
- **System 2 (S2)** — an OpenAI LLM — the generator, escalation target, and **shaper** that edits the decision/action tree between loops.

Humans define only the **input**, the **expected output**, and the **metric**. The system grows the workflow itself and converges when S2 stops needing to shape it. See [REQUIREMENT.md](REQUIREMENT.md) for the full spec and [RESEARCH.md](RESEARCH.md) for the Jev deep research.

## Quick start

Requires Python ≥ 3.10. **No dependencies** (stdlib only).

1. Put your API keys in [config.json](config.json) (copied from `config.example.json`, gitignored). **config.json is the source of truth**; the `TYPESAFE_API_KEY` / `OPENAI_API_KEY` environment variables are used only as a fallback when the file has no real key.
2. Run:

   ```
   python run.py
   ```

3. Open **http://127.0.0.1:8642**.

**Create your own task:** click **+ New task** and answer three plain-language questions — what the system should do, what a good result looks like, and (optionally) how to judge it — plus optional example inputs. S2 designs the initial decision/action tree for you (a generic template is used if S2 is unavailable); from then on the loop runs and improves it. Each task has its own tree versions, run history, and metrics — switch tasks with the dropdown in the header.

**Run loops:** either:
   - paste several task inputs (**one per line**) and click **Run batch** — each line becomes one loop, or
   - click **Auto-stream N** — the system generates N *new, distinct* same-kind task instances and loops each one.

**Every loop processes a different task input** (same *kind* of task, never the same instance — REQUIREMENT.md R-3.0). Shaping is judged across the input distribution, not against one memorized example. When S2 is live it synthesizes realistic new instances for auto-stream; in mock mode the sample pool is cycled with detail variation.

**Mock mode:** any backend without a working key (missing, invalid, or out of credits) automatically falls back to a deterministic mock so the whole loop is demoable offline. The header badges show `LIVE`/`MOCK` per system (hover a MOCK badge for the reason). The mock Jev is a crude keyword classifier; the mock LLM returns canned replies; the mock shaper never edits.

## What one loop does

```
task stream (a DIFFERENT input every loop)
   │
   ▼
decision/action tree (versioned JSON in data/tree_versions/)
   ├─ jev nodes ── S1 judgements: route / gate / validate (one batched call)
   │     └─ low confidence? ──► escalate to S2 node          (R-3.2)
   ├─ llm nodes ── S2 generation steps (Jev can't generate)  (R-2.3)
   └─ code nodes ─ S2-authored executable operations         (R-2.2)
   │
   ▼
result ──► evaluation: code metric (S2-authored) + S1 Jev battery   (R-4.1)
   │
   ▼
shaping: S2 reads tree + recent runs ──► edits tree (new version) or declines  (R-5.1)
```

Every run is logged to `data/runs.jsonl` with the full trace (answers, probabilities, confidences, gates fired, latencies). The UI shows convergence indicators: pass rate, escalation rate, shaping-edit count (R-6.3).

## Layout

| Path | What |
|---|---|
| `closeloop/jev.py` | S1 client — `POST /v1/systemone`, retry/backoff, mock fallback |
| `closeloop/llm.py` | S2 client — OpenAI chat completions, mock fallback |
| `closeloop/tree.py` | Tree schema, structural validator, runtime (walks nodes, owns control flow) |
| `closeloop/engine.py` | run → evaluate → shape loop; convergence metrics |
| `closeloop/store.py` | Tree versions + append-only run records under `data/` |
| `closeloop/server.py` | Zero-dependency HTTP server + JSON API |
| `trees/seed.json` | Seed tree: customer-support demo (triage → draft → validate → finalize) |
| `ui/index.html` | Single-page UI: **live system graph** (task stream → tree → result → evaluate → shape ↺), loop history, traces, metrics |

## Logging

Comprehensive leveled logs (DEBUG / INFO / WARNING / ERROR) via `closeloop/logsetup.py`. Two sinks:

- **Console** at `console_level` (default INFO) — the readable operator view: loop start/end (pass/fail, path, escalation, duration), shaping decisions, S1/S2 call summaries with latency + tokens, user activity, warnings/errors.
- **File** at `file_level` (default DEBUG) — `data/closeloop.log`, rotating (5 MB × 5). The comprehensive record: every Jev/LLM request and response preview, per-node steps, routing/gate decisions, per-check evaluation scores, TypeScript compile/exec, tree saves.

Configure in [config.json](config.json):

```json
"logging": { "console_level": "INFO", "file_level": "DEBUG", "file": "data/closeloop.log" }
```

Set `console_level` to `DEBUG` to see everything live, or `WARNING` for quiet operation. Loggers are namespaced `closeloop.<module>` (`closeloop.jev`, `closeloop.llm`, `closeloop.tree`, `closeloop.engine`, `closeloop.server`, `closeloop.store`, `closeloop.tscode`).

## Prototype limitations (known, deliberate)

- Code nodes run via `exec` with a trimmed builtins dict — **not a real sandbox** (REQUIREMENT.md open question 2). Don't paste untrusted trees.
- The shaper replaces the whole tree per edit; no diff-level review yet.
- Single tree, single task type; no concurrency beyond a global lock.
- Mock Jev confidence is a heuristic, not calibrated — treat mock-mode metrics as plumbing checks only.

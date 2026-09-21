"""Node schemas + capability catalog (REQUIREMENT.md §20.5, §22; scoreboard C6/C8/I).

The single source of truth for node kinds — consumed by S2 (builds nodes from
the catalog), the compile gate (validates against NODE_SCHEMAS), and the UI.
Node kinds: jev / llm / code / tool. The code node covers switch/filter/
merge/aggregate on JSON (no extra kinds).
"""
import json

NODE_SCHEMAS = {
    "jev": {
        "doc": "System 1 decision. JUDGES meaning (classify/score/route). Cannot generate or compute.",
        "required": ["questions"],
        "optional": ["gates", "route", "next", "name", "intent", "on_error"],
        "fields": {
            "questions": "object {key: {type: noul|choice|score, instructions, criteria?}}. "
                         "choice criteria = {option: description}; score criteria = [levels]; noul criteria optional {true,false}.",
            "gates": "array of {question, min_confidence(0..1), escalate_to: nodeId}.",
            "route": "{on: <questionKey> | 'inputs.x.field' | 'vars.k.field', branches:{value:nodeId}, default:nodeId} "
                     "or {on, noul_threshold, if_true, if_false}. `on` may reference a plain JSON field for a deterministic switch.",
            "next": "nodeId when no route matches (or null).",
        },
    },
    "llm": {
        "doc": "System 2 generation. The ONLY node that generates natural language / novel content. Returns a JSON object.",
        "required": ["prompt"],
        "optional": ["system", "output_key", "output_port", "output_schema", "next", "name", "intent", "on_error"],
        "fields": {
            "system": "system prompt.",
            "prompt": "user prompt; {inputs.<name>}, {vars.<key>} and {outputs.<port>} (a port already filled "
                      "by an earlier node) are substituted with JSON text.",
            "output_key": "vars key to store the returned JSON object under (default 'output').",
            "output_port": "OPTIONAL: name of an output port to fill with this node's JSON object.",
            "output_schema": "object naming the JSON fields to return, e.g. {\"tag\":\"...\",\"reason\":\"...\"}.",
            "next": "nodeId to run next (or null).",
        },
    },
    "code": {
        "doc": "Deterministic TypeScript glue: switch/filter/merge/aggregate on JSON. Cannot judge meaning or write prose.",
        "required": ["source"],
        "optional": ["next", "name", "intent", "on_error"],
        "fields": {
            "source": "TypeScript `function run(ctx: Ctx): void`. Ctx = {inputs: Record<string,Json>; vars: Record<string,Json>; "
                      "outputs: Record<string,Json>}. Read inputs/vars, write vars and outputs[<portName>]. NO import/require. tsc --strict must pass.",
            "next": "nodeId to run next (or null).",
        },
    },
    "tool": {
        "doc": "Invoke a host tool the models cannot do (OCR/vision/transcribe/render_doc/image_gen/tts/chart/code_exec/http). Wired by S2, implemented by the host.",
        "required": ["tool"],
        "optional": ["args", "output_key", "output_port", "next", "name", "intent", "on_error"],
        "fields": {
            "tool": "registered tool name (see TOOLS in the catalog).",
            "args": "object of arguments (validated against the tool's arg schema); may reference "
                    "{inputs.x}/{vars.y}/{outputs.<port>}.",
            "output_key": "vars key to store the tool's JSON result under.",
            "output_port": "OPTIONAL: output port to fill with the tool's result (e.g. an image/audio asset).",
            "next": "nodeId to run next.",
        },
    },
}


def build_catalog(contract, tool_catalog):
    """Compile the capability catalog handed to S2 for one task's multi-port contract."""
    inputs = contract.get("inputs", [])
    outputs = contract.get("outputs", [])
    L = []
    L.append("=== CAPABILITY CATALOG (build strictly against this) ===")
    L.append("")
    L.append("THIS TASK CONTRACT (fixed — you may NOT add/remove/rename/retype ports or change the metric):")
    L.append("INPUT PORTS (read via {inputs.<name>}). One bundle = one loop: each is one whole value.")
    for p in inputs:
        proj = _projection_hint(p["type"])
        L.append(f"  - {p['name']} : type={p['type']} → projection seen: {proj}")
    L.append("OUTPUT PORTS (your tree MUST fill every required one):")
    for p in outputs:
        L.append(f"  - {p['name']} : type={p['type']} → fill via an llm/code/tool node's output_port=\"{p['name']}\" "
                 f"(shape: {_output_hint(p['type'])})")
    L.append("")
    L.append("DATA MODEL: ctx = {inputs:{name→json}, vars:{}, outputs:{name→json}}. Each loop = one bundle of inputs → tree → filled outputs.")
    L.append("REFERENCING: an llm prompt / tool args / code node may read any already-filled value via "
             "{inputs.<name>}, {vars.<key>}, or {outputs.<port>}. To assemble a later output from earlier ones "
             "(e.g. render a PDF from a summary + actions), reference those ports with {outputs.<port>} — do NOT "
             "paste the literal token expecting the model to fill it.")
    L.append("")
    L.append("NODE KINDS:")
    for kind, spec in NODE_SCHEMAS.items():
        L.append(f"- {kind}: {spec['doc']}")
        L.append(f"    required={spec['required']} optional={spec['optional']}")
        for fk, fv in spec["fields"].items():
            L.append(f"    · {fk}: {fv}")
    L.append("Give every node a short `name` and `intent`. Every node may set on_error: abort|{escalate:nodeId}|skip.")
    L.append("")
    L.append("TOOLS (use via a tool node):")
    for n, t in (tool_catalog or {}).items():
        L.append(f"  - {n}: {t['doc']} args={t['args']}")
    L.append("=== END CATALOG ===")
    return "\n".join(L)


def _projection_hint(t):
    return {
        "text": "the string",
        "json": "the JSON record",
        "table": "{rows:[...], columns:[...]} — the whole sheet; iterate rows inside the loop if needed",
        "document": "{text: extracted text}",
        "image": "{caption: caption+OCR text}",
        "audio": "{transcript: text}",
    }.get(t, "json")


def _output_hint(t):
    return {
        "text": "{text: ...} or any JSON",
        "json": "{...fields...}",
        "table_enrich": "{rows:[ {col: value}, ... ]} — the full result rows; written to a new file",
        "document": "{markdown: ...} (rendered to PDF/DOCX) or {asset} from render_doc",
        "image": "{asset} from an image_gen tool node",
        "audio": "{asset} from a tts tool node",
        "action": "{action: ..., args: {...}} (performed by a tool node; recorded)",
    }.get(t, "{...}")


def validate_node(nid, node):
    """Return schema errors for one node (compile-gate helper): known kind + required fields."""
    kind = node.get("kind")
    spec = NODE_SCHEMAS.get(kind)
    if spec is None:
        return [f"node '{nid}': unknown kind '{kind}'"]
    return [f"node '{nid}' ({kind}): missing required field '{req}'"
            for req in spec["required"] if not node.get(req)]

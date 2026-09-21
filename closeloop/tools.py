"""Host tools (REQUIREMENT.md §16.1, scoreboard G3T) — operations the models
cannot do themselves. Each tool takes JSON args and returns JSON (and may write
assets). Tools are whitelisted infrastructure; S2 wires them via a `tool` node
but never implements them.

Local tools (deterministic, verifiable offline): render_doc, chart, code_exec, http.
OpenAI-backed tools (live API calls; models configurable in config.json → "tools"):
vision (caption/OCR), transcribe (ASR), image_gen, tts.
"""
import base64
import io
import json
import os
import urllib.error
import urllib.request

from . import assets, logsetup
from .config import ROOT

log = logsetup.get("tools")

# --- config -----------------------------------------------------------------
_CFG = {}          # set by init(cfg)
_TOOL_MODELS = {}  # openai model ids per multimodal tool


def init(cfg):
    """Called once at startup with the loaded config."""
    global _CFG, _TOOL_MODELS
    _CFG = cfg
    t = cfg.get("tools", {})
    # No placeholder/guessed model ids: vision defaults to the configured chat
    # model (real); transcribe/image_gen/tts must be set explicitly in
    # config.json → "tools" or the tool refuses to run (no stand-in).
    _TOOL_MODELS = {
        "vision": t.get("vision_model") or cfg["openai"].get("model"),
        "transcribe": t.get("transcribe_model"),
        "image_gen": t.get("image_model"),
        "tts": t.get("tts_model"),
    }


def _model(name):
    m = _TOOL_MODELS.get(name)
    if not m:
        raise RuntimeError(f"tool '{name}' needs a model id — set tools.{name}_model in config.json "
                           "(no default is guessed).")
    return m


# --- OpenAI helpers ---------------------------------------------------------
def _oa_url(path):
    return _CFG["openai"]["base_url"].rstrip("/") + path


def _oa_headers(extra=None):
    h = {"Authorization": "Bearer " + _CFG["openai"]["api_key"]}
    if extra:
        h.update(extra)
    return h


def _post_json(path, payload):
    req = urllib.request.Request(_oa_url(path), data=json.dumps(payload).encode(),
                                 headers=_oa_headers({"Content-Type": "application/json"}), method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_multipart(path, fields, files):
    """fields: {name: str}; files: {name: (filename, bytes, mime)}."""
    boundary = "----closeloopFormBoundary7MA4YWxkTrZu0gW"
    body = io.BytesIO()
    for name, val in fields.items():
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n".encode())
    for name, (fn, data, mime) in files.items():
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fn}\"\r\n".encode())
        body.write(f"Content-Type: {mime}\r\n\r\n".encode())
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(_oa_url(path), data=body.getvalue(),
                                 headers=_oa_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


# --- tool implementations ---------------------------------------------------
def _vision(args):
    """Caption + OCR an image asset via a vision-capable chat model."""
    ref = args["asset"]
    data = assets.get_bytes(ref["asset_id"])
    b64 = base64.b64encode(data).decode()
    prompt = args.get("prompt", "Describe this image in detail and transcribe any text you can read (OCR).")
    payload = {
        "model": _model("vision"),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{ref.get('mime','image/png')};base64,{b64}"}},
        ]}],
    }
    data = _post_json("/chat/completions", payload)
    return {"text": data["choices"][0]["message"]["content"]}


def _transcribe(args):
    """Transcribe an audio asset (ASR)."""
    ref = args["asset"]
    audio = assets.get_bytes(ref["asset_id"])
    raw = _post_multipart("/audio/transcriptions",
                          {"model": _model("transcribe")},
                          {"file": (ref.get("name", "audio.mp3"), audio, ref.get("mime", "audio/mpeg"))})
    try:
        return {"text": json.loads(raw).get("text", "")}
    except json.JSONDecodeError:
        return {"text": raw.decode("utf-8", "ignore")}


def _image_gen(args):
    """Generate an image from a text prompt; returns an image asset ref."""
    data = _post_json("/images/generations", {
        "model": _model("image_gen"), "prompt": args["prompt"],
        "size": args.get("size", "1024x1024"), "n": 1,
    })
    item = data["data"][0]
    img = base64.b64decode(item["b64_json"]) if item.get("b64_json") else _fetch(item["url"])
    ref = assets.put(img, name=args.get("name", "generated.png"), mime="image/png",
                     meta={"tool": "image_gen", "prompt": args["prompt"][:200]})
    return {"asset": ref}


def _tts(args):
    """Text to speech; returns an audio asset ref."""
    audio = _post_json_raw("/audio/speech", {
        "model": _model("tts"), "input": args["text"], "voice": args.get("voice", "alloy"),
        "response_format": "mp3",
    })
    ref = assets.put(audio, name=args.get("name", "speech.mp3"), mime="audio/mpeg", meta={"tool": "tts"})
    return {"asset": ref}


def _post_json_raw(path, payload):
    req = urllib.request.Request(_oa_url(path), data=json.dumps(payload).encode(),
                                 headers=_oa_headers({"Content-Type": "application/json"}), method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def _fetch(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def _render_doc(args):
    """Render Markdown/HTML to a PDF or DOCX artifact; returns an asset ref."""
    fmt = args.get("format", "pdf").lower()
    text = args.get("markdown") or args.get("text") or ""
    name = args.get("name", "document")
    if fmt == "docx":
        import docx
        doc = docx.Document()
        for line in text.split("\n"):
            if line.startswith("# "):
                doc.add_heading(line[2:], level=1)
            elif line.startswith("## "):
                doc.add_heading(line[3:], level=2)
            elif line.strip():
                doc.add_paragraph(line)
        buf = io.BytesIO()
        doc.save(buf)
        return {"asset": assets.put(buf.getvalue(), name=f"{name}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                meta={"tool": "render_doc"})}
    # PDF via reportlab (robust; fpdf clashes with a legacy PyFPDF install)
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    styles = getSampleStyleSheet()

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    story = []
    for line in text.split("\n"):
        if line.startswith("# "):
            story.append(Paragraph(esc(line[2:]), styles["Title"]))
        elif line.startswith("## "):
            story.append(Paragraph(esc(line[3:]), styles["Heading2"]))
        elif line.strip():
            story.append(Paragraph(esc(line), styles["BodyText"]))
        else:
            story.append(Spacer(1, 6))
    doc.build(story or [Spacer(1, 6)])
    return {"asset": assets.put(buf.getvalue(), name=f"{name}.pdf", mime="application/pdf", meta={"tool": "render_doc"})}


def _chart(args):
    """Render a simple chart from a spec {type, labels, values, title}; returns an image asset."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    spec = args.get("spec", args)
    labels = spec.get("labels", [])
    values = spec.get("values", [])
    fig, ax = plt.subplots(figsize=(6, 4))
    kind = spec.get("type", "bar")
    if kind == "line":
        ax.plot(labels, values, marker="o")
    elif kind == "pie":
        ax.pie(values, labels=labels, autopct="%1.0f%%")
    else:
        ax.bar(labels, values)
    if spec.get("title"):
        ax.set_title(spec["title"])
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return {"asset": assets.put(buf.getvalue(), name=spec.get("name", "chart") + ".png", mime="image/png",
            meta={"tool": "chart"})}


def _code_exec(args):
    """Execute a TypeScript snippet `function run(input: Json): Json` on JSON input."""
    from . import tscode
    return {"result": tscode.run_snippet(args["source"], args.get("input"))}


def _http(args):
    """Make an allowlisted HTTP request. Host must be in config.tools.http_allowlist."""
    url = args["url"]
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    allow = _CFG.get("tools", {}).get("http_allowlist", [])
    if host not in allow:
        raise PermissionError(f"host '{host}' not in tools.http_allowlist {allow}")
    method = args.get("method", "GET").upper()
    body = json.dumps(args["json"]).encode() if args.get("json") is not None else None
    req = urllib.request.Request(url, data=body, headers=args.get("headers", {}), method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    try:
        return {"status": 200, "json": json.loads(raw)}
    except json.JSONDecodeError:
        return {"status": 200, "text": raw[:5000]}


# --- registry ---------------------------------------------------------------
TOOLS = {
    "vision":      {"fn": _vision,      "doc": "Caption + OCR an image asset.", "args": {"asset": "image asset ref", "prompt": "optional"}, "kind": "openai"},
    "transcribe":  {"fn": _transcribe,  "doc": "Transcribe an audio asset (ASR).", "args": {"asset": "audio asset ref"}, "kind": "openai"},
    "image_gen":   {"fn": _image_gen,   "doc": "Generate an image from a prompt.", "args": {"prompt": "text", "size": "optional"}, "kind": "openai"},
    "tts":         {"fn": _tts,         "doc": "Text to speech (audio asset).", "args": {"text": "text", "voice": "optional"}, "kind": "openai"},
    "render_doc":  {"fn": _render_doc,  "doc": "Render Markdown to PDF or DOCX.", "args": {"markdown": "text", "format": "pdf|docx", "name": "optional"}, "kind": "local"},
    "chart":       {"fn": _chart,       "doc": "Render a bar/line/pie chart image.", "args": {"spec": "{type,labels,values,title}"}, "kind": "local"},
    "code_exec":   {"fn": _code_exec,   "doc": "Run a TypeScript run(input) snippet.", "args": {"source": "TS", "input": "json"}, "kind": "local"},
    "http":        {"fn": _http,        "doc": "Allowlisted HTTP request.", "args": {"url": "...", "method": "GET|POST", "json": "optional"}, "kind": "local"},
}


def run(name, args):
    if name not in TOOLS:
        raise ValueError(f"unknown tool '{name}' (available: {sorted(TOOLS)})")
    log.info("tool '%s' args=%s", name, logsetup.preview(args, 160))
    result = TOOLS[name]["fn"](args or {})
    log.debug("tool '%s' -> %s", name, logsetup.preview(result, 200))
    return result


def catalog():
    """Tool descriptions for the S2 capability catalog."""
    return {n: {"doc": t["doc"], "args": t["args"], "kind": t["kind"]} for n, t in TOOLS.items()}

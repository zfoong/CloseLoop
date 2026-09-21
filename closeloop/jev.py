"""System 1 client — TypeSafe Jev via POST /v1/systemone (live only, no mock).

Request/response shapes follow docs.typesafe.ai (primitives/choice|score|noul):
  request:  {state, model, questions: {key: {type, instructions, criteria?}}}
  response: {model, answers: {key: <typed answer>}, usage: {input_tokens, output_tokens}}
"""
import json
import time
import urllib.error
import urllib.request

from . import logsetup

log = logsetup.get("jev")


class JevClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def system_one(self, state, questions):
        """Call Jev. Returns (answers: dict, meta: dict). Raises on API error."""
        qkeys = list(questions.keys())
        state_len = len(json.dumps(state, default=str))
        log.debug("S1 call: %d question(s) %s | state=%d chars", len(qkeys), qkeys, state_len)

        body = json.dumps({
            "state": state,
            "model": self.cfg.get("model", "jev-latest"),
            "questions": questions,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.cfg["base_url"].rstrip("/") + "/systemone",
            data=body,
            headers={
                "Authorization": "Bearer " + self.cfg["api_key"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        start = time.time()
        backoff = 1.0
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                latency = int((time.time() - start) * 1000)
                usage = data.get("usage", {})
                log.info("S1 ok: %d question(s) | %dms | tokens in=%s out=%s",
                         len(qkeys), latency, usage.get("input_tokens", "?"), usage.get("output_tokens", "?"))
                log.debug("S1 answers: %s", logsetup.preview(data.get("answers"), 400))
                return data["answers"], {"latency_ms": latency, "usage": usage}
            except urllib.error.HTTPError as e:
                if e.code in (429, 529) and attempt < 3:
                    log.warning("S1 %d (rate/overload) — backoff %.1fs (attempt %d/3)", e.code, backoff, attempt + 1)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                detail = e.read().decode("utf-8", "ignore")[:300]
                log.error("S1 API error %d: %s", e.code, detail)
                raise RuntimeError(f"Jev API error {e.code}: {detail}")
            except urllib.error.URLError as e:
                log.error("S1 network error: %s", e)
                raise RuntimeError(f"Jev network error: {e}")
        log.error("S1 retries exhausted")
        raise RuntimeError("Jev API: retries exhausted")

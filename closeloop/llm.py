"""System 2 client — OpenAI chat completions (POST /v1/chat/completions). Live only.

Used four ways in CloseLoop:
  1. as an operation node inside the tree (generation steps),
  2. as the escalation target when a Jev gate fires,
  3. as the ARCHITECT that designs a task's initial tree,
  4. as the SHAPER that edits the tree between loops.
"""
import json
import time
import urllib.error
import urllib.request

from . import logsetup

log = logsetup.get("llm")


class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg

    def complete(self, system, user, force_json=False):
        """Call the LLM. Returns (text, meta). Raises on API error."""
        role = "shaper" if "SHAPER" in system else ("architect" if "ARCHITECT" in system else "generation")
        log.debug("S2 call [%s]: model=%s json=%s | system=%d chars user=%d chars | user: %s",
                  role, self.cfg.get("model", "?"), force_json, len(system), len(user),
                  logsetup.preview(user, 160))

        payload = {
            "model": self.cfg.get("model", "gpt-6-astra"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if force_json:
            payload["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            self.cfg["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.cfg["api_key"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        start = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            log.error("S2 API error %d: %s", e.code, detail)
            raise RuntimeError(f"OpenAI API error {e.code}: {detail}")
        except urllib.error.URLError as e:
            log.error("S2 network error: %s", e)
            raise RuntimeError(f"OpenAI network error: {e}")
        text = data["choices"][0]["message"]["content"]
        latency = int((time.time() - start) * 1000)
        usage = data.get("usage", {})
        log.info("S2 ok [%s]: %dms | tokens prompt=%s completion=%s",
                 role, latency, usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
        log.debug("S2 response [%s]: %s", role, logsetup.preview(text, 300))
        return text, {"latency_ms": latency, "usage": usage}

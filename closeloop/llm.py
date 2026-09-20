"""System 2 client — OpenAI chat completions (POST /v1/chat/completions).

Used three ways in CloseLoop (see REQUIREMENT.md):
  1. as an operation node inside the tree (generation steps),
  2. as the escalation target when a Jev gate fires,
  3. as the shaper that edits the tree between loops.

Falls back to a canned mock when no API key is configured.
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
        self.mock = cfg.get("mock", True)
        self.fallback_reason = None

    def complete(self, system, user, force_json=False):
        """Returns (text, meta)."""
        role = "shaper" if "SHAPER" in system else ("architect" if "ARCHITECT" in system else "generation")
        log.debug("S2 call [%s]: model=%s json=%s | system=%d chars user=%d chars | user: %s",
                  role, self.cfg.get("model", "?"), force_json, len(system), len(user),
                  logsetup.preview(user, 160))

        if self.mock:
            meta = {"mock": True, "latency_ms": 0}
            if self.fallback_reason:
                meta["fallback"] = self.fallback_reason
            text = self._mock(system, user, force_json)
            log.debug("S2 mock [%s] -> %s", role, logsetup.preview(text, 160))
            return text, meta

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
            # Bad key or exhausted credits: degrade to mock for the rest of the
            # session so the loop stays alive, and surface WHY in every meta.
            if e.code in (401, 403) or (e.code == 429 and ("quota" in detail.lower() or "credit" in detail.lower())):
                self.mock = True
                self.fallback_reason = f"OpenAI {e.code}: {detail[:120]} — fell back to mock"
                log.warning("S2 %d (auth/quota) — falling back to MOCK for the session: %s", e.code, detail[:120])
                return self.complete(system, user, force_json)
            log.error("S2 API error %d: %s", e.code, detail)
            raise RuntimeError(f"OpenAI API error {e.code}: {detail}")
        except urllib.error.URLError as e:
            log.error("S2 network error: %s", e)
            raise RuntimeError(f"OpenAI network error: {e}")
        text = data["choices"][0]["message"]["content"]
        latency = int((time.time() - start) * 1000)
        usage = data.get("usage", {})
        meta = {"mock": False, "latency_ms": latency, "usage": usage}
        log.info("S2 ok [%s]: %dms | tokens prompt=%s completion=%s",
                 role, latency, usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
        log.debug("S2 response [%s]: %s", role, logsetup.preview(text, 300))
        return text, meta

    # ------------------------------------------------------------------
    def _mock(self, system, user, force_json):
        if "SHAPER" in system:
            # Mock shaper: leave the tree alone (converged behavior).
            return json.dumps({"shape": False, "reason": "mock mode: no failures observed, no edit needed"})
        # Mock generation is a JSON object (LLM nodes always emit JSON). Uses the
        # conventional "output" field so template/bootstrap trees work offline.
        snippet = " ".join(user.split())[:160]
        return json.dumps({
            "output": (f"Re: \"{snippet}…\" — reviewed; we'll resolve this promptly. (mock S2 output)"),
            "mock": True,
        })

"""System 1 client — TypeSafe Jev via POST /v1/systemone.

Request/response shapes follow docs.typesafe.ai (primitives/choice|score|noul):
  request:  {state, model, questions: {key: {type, instructions, criteria?}}}
  response: {model, answers: {key: <typed answer>}, usage: {input_tokens, output_tokens}}

Falls back to a deterministic mock when no API key is configured, so the
prototype loop and UI work before Jev access is granted.
"""
import hashlib
import json
import time
import urllib.error
import urllib.request

from . import logsetup

log = logsetup.get("jev")


class JevClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mock = cfg.get("mock", True)
        self.fallback_reason = None

    def system_one(self, state, questions):
        """Returns (answers: dict, meta: dict). Never raises on mock."""
        qkeys = list(questions.keys())
        state_len = len(json.dumps(state, default=str))
        log.debug("S1 call: %d question(s) %s | state=%d chars | mock=%s",
                  len(qkeys), qkeys, state_len, self.mock)

        if self.mock:
            meta = {"mock": True, "latency_ms": 0}
            if self.fallback_reason:
                meta["fallback"] = self.fallback_reason
            answers = self._mock(state, questions)
            log.debug("S1 mock answers: %s", logsetup.preview(answers, 300))
            return answers, meta

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
                meta = {"mock": False, "latency_ms": latency, "usage": usage}
                log.info("S1 ok: %d question(s) | %dms | tokens in=%s out=%s",
                         len(qkeys), latency, usage.get("input_tokens", "?"), usage.get("output_tokens", "?"))
                log.debug("S1 answers: %s", logsetup.preview(data.get("answers"), 400))
                return data["answers"], meta
            except urllib.error.HTTPError as e:
                if e.code in (429, 529) and attempt < 3:
                    log.warning("S1 %d (rate/overload) — backoff %.1fs (attempt %d/3)", e.code, backoff, attempt + 1)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                detail = e.read().decode("utf-8", "ignore")[:300]
                if e.code in (401, 403):
                    self.mock = True
                    self.fallback_reason = f"Jev {e.code}: {detail[:120]} — fell back to mock"
                    log.warning("S1 %d (auth) — falling back to MOCK for the session: %s", e.code, detail[:120])
                    return self.system_one(state, questions)
                log.error("S1 API error %d: %s", e.code, detail)
                raise RuntimeError(f"Jev API error {e.code}: {detail}")
            except urllib.error.URLError as e:
                log.error("S1 network error: %s", e)
                raise RuntimeError(f"Jev network error: {e}")
        log.error("S1 retries exhausted")
        raise RuntimeError("Jev API: retries exhausted")

    # ------------------------------------------------------------------
    # Mock: deterministic pseudo-probabilities derived from hashing the
    # state+question, so runs are repeatable and confidence gates fire
    # believably (most answers confident, some ambiguous).
    # ------------------------------------------------------------------
    def _mock(self, state, questions):
        """Crude keyword classifier: overlap between state words and option/
        instruction words drives probabilities, hash noise breaks ties. Makes
        mock routing believable (a billing message routes to billing)."""
        answers = {}
        state_str = json.dumps(state, sort_keys=True, default=str)
        state_words = _keywords(state_str)
        for key, q in questions.items():
            qtype = q["type"]
            if qtype == "noul":
                text = json.dumps(q.get("instructions", "")) + json.dumps(q.get("criteria", ""))
                ov = _overlap(state_words, _keywords(text))
                seed = _unit(state_str + key)
                if ov > 0:  # question is about something present in the state -> lean yes
                    noul = min(0.97, 0.62 + 0.08 * ov + 0.15 * seed)
                else:
                    noul = _shape(seed)
                answers[key] = {"type": "noul", "noul": round(noul, 3)}
            elif qtype == "choice":
                weights = {}
                for opt, desc in q["criteria"].items():
                    ov = _overlap(state_words, _keywords(opt + " " + json.dumps(desc, default=str)))
                    weights[opt] = (0.25 + ov) ** 2 + 0.1 * _unit(state_str + key + opt)
                total = sum(weights.values())
                probs = {o: round(w / total, 3) for o, w in weights.items()}
                best = max(probs, key=probs.get)
                answers[key] = {
                    "type": "choice",
                    "choice": best,
                    "probabilities": probs,
                    "confidence": round(_confidence(list(probs.values())), 3),
                }
            elif qtype == "score":
                levels = [str(i) for i in range(len(q["criteria"]))]
                probs = _mock_distribution(levels, state_str + key)
                score = sum(float(k) * v for k, v in probs.items())
                answers[key] = {
                    "type": "score",
                    "score": round(score, 2),
                    "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                    "probabilities": probs,
                    "confidence": round(_confidence(list(probs.values())), 3),
                }
        return answers


def _unit(text):
    """Deterministic float in [0,1) from text."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big") / 2**64


def _keywords(text):
    words = set()
    for raw in text.lower().split():
        w = "".join(c for c in raw if c.isalpha())
        if len(w) >= 4:
            words.add(w)
    return words


def _overlap(a, b):
    """Count word pairs matching on a 4-char prefix (charged ~ charges)."""
    count = 0
    for wa in a:
        for wb in b:
            if wa[:4] == wb[:4]:
                count += 1
                break
    return count


def _shape(x):
    """Push values toward 0/1 so mock answers look calibrated-decisive."""
    if x < 0.4:
        return x * 0.5            # 0 .. 0.2
    if x > 0.6:
        return 0.8 + (x - 0.6) * 0.5  # 0.8 .. 1.0
    return x                      # ambiguous middle band


def _mock_distribution(options, seed_text):
    weights = [_unit(seed_text + o) ** 3 for o in options]  # cube → peaky
    total = sum(weights) or 1.0
    return {o: round(w / total, 3) for o, w in zip(options, weights)}


def _confidence(probs):
    """Top-probability margin as a simple confidence proxy."""
    s = sorted(probs, reverse=True)
    return s[0] if len(s) == 1 else s[0] - s[1] * 0.5

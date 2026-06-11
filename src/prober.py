"""Live probe: send a REAL chat request to a credential and classify the result.

The key correctness rule (learned from the data): a probe FAILURE is only a weak
signal, because

  * no-prefix entries are POOLED (a bare gpt-5.5 request is routed by fill-first
    across many keys), so a probe cannot be attributed to one specific key; and
  * some upstreams return 403/Cloudflare or 502 "unknown provider" that reflect
    routing/anti-bot quirks, not a dead key (e.g. codex-muyuan: 403 on probe but
    20% fail in the usage DB).

So we classify probe outcomes into three buckets and let the scorer combine them
with DB history rather than trusting a single probe:

  OK            -> got real assistant text (strong positive)
  HARD_FAIL     -> upstream clearly rejected us: invalid token / quota / auth
                   unavailable / cloudflare block (negative, but still cross-checked
                   against DB before killing a credential)
  INCONCLUSIVE  -> routing artifacts (502 unknown provider, model_not_found),
                   timeouts, or pooled entries we can't attribute -> ignore probe,
                   use DB only
"""
import json
import time
from urllib import request as urlreq
from urllib import error as urlerr


OK = "OK"
# A credential is clearly broken in a way that does NOT fix itself: the key was
# revoked, is invalid, the account was disabled, or we are blocked by anti-bot.
# These warrant demotion toward "dead".
PERM_FAIL = "PERM_FAIL"
# The credential itself is fine but is temporarily out of budget: quota window
# exhausted, rate-limited, balance depleted. These RECOVER on their own (next
# quota window / top-up), so we rest them low instead of killing them, and keep
# re-probing so they climb straight back when credit returns.
TEMP_FAIL = "TEMP_FAIL"
# Routing artifacts / upstream hiccups we must NOT hold against the credential.
INCONCLUSIVE = "INCONCLUSIVE"

# Back-compat alias: older call sites used HARD_FAIL for "clearly the cred's
# fault". It now maps to the permanent bucket.
HARD_FAIL = PERM_FAIL


# Substrings that mark a PERMANENT credential problem (case-insensitive).
_PERM_FAIL_MARKERS = (
    "invalid token",
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "api key not valid",
    "unauthorized",
    "permission",
    "account is disabled",
    "account disabled",
    "account_deactivated",
    "deactivated",
    "revoked",
    "suspended",
    "banned",
    "doctype html",       # cloudflare / anti-bot interstitial
    "access denied",
    "forbidden",
)

# Substrings that mark a TEMPORARY, self-recovering condition (case-insensitive).
_TEMP_FAIL_MARKERS = (
    "用户额度不足",
    "额度已用",
    "额度不足",
    "余额不足",
    "quota",
    "insufficient",
    "insufficient_quota",
    "rate limit",
    "rate_limit",
    "too many requests",
    "exceeded your current",
    "billing",
    "payment required",
    "balance",
    "credit",
    "usage limit",
    "limit reached",
    "请求过于频繁",
    "已达上限",
)

# Substrings that mark a routing artifact we must NOT hold against the credential.
_INCONCLUSIVE_MARKERS = (
    "unknown provider",
    "model_not_found",
    "no available channel",
    "no available channels",
    "service temporarily unavailable",
    "service unavailable",
)


def _is_image_model(name):
    n = (name or "").lower()
    return "image" in n or "imagine" in n


def classify(status, body_text, content):
    """Map an HTTP result to OK / TEMP_FAIL / PERM_FAIL / INCONCLUSIVE.

    Order of checks matters:
      1. real assistant text  -> OK
      2. explicit routing artifacts -> INCONCLUSIVE (never blame the cred)
      3. temporary markers (quota/rate-limit/balance) -> TEMP_FAIL (recoverable)
      4. permanent markers (revoked/invalid/forbidden) -> PERM_FAIL
      5. status-code fallback: 429/402 -> TEMP_FAIL; 401/403 -> PERM_FAIL
      6. everything else (5xx, timeout, empty) -> INCONCLUSIVE
    """
    if content and content.strip():
        return OK, content.strip()[:60].replace("\n", " ")

    low = (body_text or "").lower()

    for marker in _INCONCLUSIVE_MARKERS:
        if marker in low:
            return INCONCLUSIVE, (body_text or "")[:80].replace("\n", " ")

    # Temporary markers take precedence over permanent ones: a body like
    # "quota exceeded" sometimes also contains generic words; quota wins.
    for marker in _TEMP_FAIL_MARKERS:
        if marker in low:
            return TEMP_FAIL, f"{status}:{(body_text or '')[:70]}".replace("\n", " ")

    for marker in _PERM_FAIL_MARKERS:
        if marker in low:
            return PERM_FAIL, f"{status}:{(body_text or '')[:70]}".replace("\n", " ")

    # Status-code fallback when the body had no recognizable marker.
    if status == 429 or status == 402:
        return TEMP_FAIL, f"{status}:{(body_text or '')[:70]}".replace("\n", " ")
    if status in (401, 403):
        return PERM_FAIL, f"{status}:{(body_text or '')[:70]}".replace("\n", " ")

    # 5xx, timeouts, empty bodies, malformed JSON -> can't conclude.
    return INCONCLUSIVE, f"{status}:{(body_text or '')[:70]}".replace("\n", " ")


class Prober:
    def __init__(self, base_url, client_api_key, *, proxy=None, timeout=60,
                 max_tokens=64, prompt="写一个解压zip文件的python脚本，只要核心代码"):
        self.base_url = base_url.rstrip("/")
        self.client_api_key = client_api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.prompt = prompt
        if proxy:
            self._handler = urlreq.ProxyHandler({"http": proxy, "https": proxy})
        else:
            self._handler = urlreq.ProxyHandler({})

    def probe(self, route_model):
        """Send one chat request to `route_model`. Returns (bucket, status, ms, brief)."""
        body = json.dumps({
            "model": route_model,
            "messages": [{"role": "user", "content": self.prompt}],
            "max_tokens": self.max_tokens,
            "stream": False,
        }).encode("utf-8")
        url = f"{self.base_url}/v1/chat/completions"
        opener = urlreq.build_opener(self._handler)
        req = urlreq.Request(url, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.client_api_key}",
            "Content-Type": "application/json",
        })
        t0 = time.time()
        try:
            with opener.open(req, timeout=self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                dt = int((time.time() - t0) * 1000)
                content = ""
                try:
                    j = json.loads(raw)
                    choices = j.get("choices") or []
                    if choices:
                        content = (choices[0].get("message") or {}).get("content") or ""
                except (ValueError, TypeError):
                    pass
                bucket, brief = classify(r.status, raw, content)
                return bucket, r.status, dt, brief
        except urlerr.HTTPError as e:
            dt = int((time.time() - t0) * 1000)
            errbody = ""
            try:
                errbody = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            bucket, brief = classify(e.code, errbody, "")
            return bucket, e.code, dt, brief
        except Exception as exc:  # noqa: BLE001 - timeouts etc. are inconclusive
            dt = int((time.time() - t0) * 1000)
            return INCONCLUSIVE, None, dt, f"{type(exc).__name__}:{str(exc)[:60]}"


def pick_probe_model(entry, visible_models):
    """Choose a routable model id for an entry, or None if it can't be probed.

    Rules:
      * prefer a non-image chat model;
      * if the entry has a prefix, route as `<prefix>/<model>` and require that
        id to be visible in /v1/models;
      * no-prefix entries are POOLED -> not attributable -> return None (DB only).
    """
    models = [m.get("name") for m in (entry.get("models") or []) if m.get("name")]
    chat_models = [m for m in models if not _is_image_model(m)]
    pick_list = chat_models or models
    if not pick_list:
        return None, True  # nothing to probe; image-only or empty

    prefix = (entry.get("prefix") or "").strip()
    if not prefix:
        # Pooled / shared bare-model entry: a probe can't be attributed.
        return None, False

    for m in pick_list:
        route = f"{prefix}/{m}"
        if not visible_models or route in visible_models:
            return route, _is_image_model(m)
    # Prefix present but no visible route (e.g. excluded-models: ['*']).
    return None, False

"""CPA management API client (stdlib-only).

Wraps the subset of the CLIProxyAPI Management API needed to read and update
credential priority / disabled state for the four credential types the keeper
manages: codex-api-key, openai-compatibility, gemini-api-key, claude-api-key.

PATCH semantics (verified live):
  PATCH /v0/management/<type>  body {"index": <i>, "value": <full-entry>}
  -> returns {"status":"ok"} and persists priority/disabled to config.yaml.

We always send the FULL existing entry back with only priority/disabled
mutated, so no other field is lost.
"""
import json
import time
from urllib import request as urlreq
from urllib import error as urlerr


# Management path per credential type.
TYPE_PATHS = {
    "codex-api-key": "/v0/management/codex-api-key",
    "openai-compatibility": "/v0/management/openai-compatibility",
    "gemini-api-key": "/v0/management/gemini-api-key",
    "claude-api-key": "/v0/management/claude-api-key",
}


class CPAClient:
    def __init__(self, base_url, token, *, proxy=None, timeout=30, max_retries=2):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries
        self.proxy = proxy

    def _opener(self):
        if self.proxy:
            handler = urlreq.ProxyHandler({"http": self.proxy, "https": self.proxy})
        else:
            # Explicitly bypass any environment proxy for localhost management.
            handler = urlreq.ProxyHandler({})
        return urlreq.build_opener(handler)

    def _request(self, method, path, *, body=None):
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}"
        last_error = None
        opener = self._opener()
        for attempt in range(self.max_retries + 1):
            req = urlreq.Request(url, data=data, method=method, headers=headers)
            try:
                with opener.open(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8", "replace")
                    try:
                        return r.status, json.loads(raw)
                    except (ValueError, TypeError):
                        return r.status, raw
            except urlerr.HTTPError as e:
                errbody = ""
                try:
                    errbody = e.read().decode("utf-8", "replace")
                except Exception:
                    pass
                # 5xx: retry; 4xx: fail fast.
                if e.code >= 500 and attempt < self.max_retries:
                    last_error = f"HTTP {e.code}: {errbody[:200]}"
                    time.sleep(1)
                    continue
                return e.code, errbody
            except Exception as exc:  # noqa: BLE001 - surface as soft error
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    time.sleep(1)
                    continue
        return None, last_error or "request failed"

    # ---- reads -------------------------------------------------------------
    def get_entries(self, cred_type):
        """Return list of entries for a credential type, or [] on failure."""
        path = TYPE_PATHS[cred_type]
        status, data = self._request("GET", path)
        if status != 200 or not isinstance(data, dict):
            return []
        return data.get(cred_type, []) or []

    def list_models(self, client_api_key):
        """Return the set of client-visible model IDs (for probe routing)."""
        url = f"{self.base_url}/v1/models"
        opener = self._opener()
        req = urlreq.Request(url, headers={"Authorization": f"Bearer {client_api_key}"})
        try:
            with opener.open(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            return {m["id"] for m in data.get("data", []) if m.get("id")}
        except Exception:
            return set()

    # ---- writes ------------------------------------------------------------
    def put_entries(self, cred_type, entries):
        """Replace the ENTIRE list for a credential type with one atomic PUT.

        This is the ONLY way to set `priority`: the PATCH handler whitelists
        fields and silently drops priority, whereas PUT decodes the full entry
        struct (which includes priority). We always build `entries` from a
        verbatim GET of the same list with only priority/disabled mutated, so
        no other field is lost.

        Returns (ok: bool, detail: str).
        """
        path = TYPE_PATHS[cred_type]
        status, data = self._request("PUT", path, body=entries)
        if status == 200:
            return True, "ok"
        detail = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        return False, f"HTTP {status}: {str(detail)[:200]}"

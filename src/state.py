"""Persistent per-credential state across rounds (JSON file).

Why this exists: a single probe is noisy. To avoid flapping a credential's
priority up and down every hour, and to implement recovery hysteresis, we
remember a little history per credential between rounds:

  * ok_streak    - consecutive rounds the credential probed OK
  * fail_streak  - consecutive rounds it permanently failed
  * temp_streak  - consecutive rounds it was temporarily out of budget
  * last_bucket  - the last probe bucket we saw
  * last_tier    - the last tier we assigned
  * last_priority- the last priority we wrote
  * dead_since   - ISO timestamp when it was first marked dead (for reporting)
  * first_seen   - ISO timestamp the credential first appeared

Keyed by a stable credential key: prefer the config auth-index, else a hash of
the api-key, else "<cred_type>#<base-url>". The file is small, human-readable,
and rewritten atomically each round.
"""
import hashlib
import json
import os
import tempfile
import threading
import time


def _hash_key(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class KeeperState:
    def __init__(self, path):
        self.path = path
        self._data = {"version": 1, "entries": {}}
        self._dirty = False
        self._lock = threading.Lock()

    # ---- stable key --------------------------------------------------------
    def key_for(self, cred_type, entry, auth_idxs, api_keys):
        """Return a stable cross-round key for a credential.

        Preference order so a reordered or re-saved config still matches:
          1. config auth-index (server-stable hash of the credential)
          2. hash of the first api-key
          3. cred_type + base-url + prefix (last-resort, position-independent)
        """
        if auth_idxs:
            return f"{cred_type}:ai:{auth_idxs[0]}"
        if api_keys:
            return f"{cred_type}:ak:{_hash_key(api_keys[0])}"
        base = (entry.get("base-url") or "").strip()
        prefix = (entry.get("prefix") or "").strip()
        name = (entry.get("name") or "").strip()
        return f"{cred_type}:meta:{_hash_key(name + '|' + base + '|' + prefix)}"

    # ---- load / save -------------------------------------------------------
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
            if isinstance(raw, dict) and isinstance(raw.get("entries"), dict):
                self._data = raw
                self._data.setdefault("version", 1)
        except (FileNotFoundError, ValueError, OSError):
            # Missing or corrupt state is non-fatal: start fresh.
            self._data = {"version": 1, "entries": {}}
        return self

    def save(self):
        if not self._dirty:
            return
        self._data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            # Atomic write: temp file in the same dir, then replace.
            fd, tmp = tempfile.mkstemp(prefix=".state-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fp:
                    json.dump(self._data, fp, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            self._dirty = False
        except OSError:
            # Never let a state-save failure break a round.
            pass

    # ---- accessors ---------------------------------------------------------
    def get(self, key):
        """Return the prior streak counters for a credential.

        The scorer OWNS the counter transitions; this is a dumb read of what the
        last round persisted. `condemn_streak` is the unified dead-evidence
        counter (replaces the old split fail_streak). Legacy `fail_streak` from
        older state files is read as a fallback so an upgrade does not reset it.
        """
        with self._lock:
            entry = self._data["entries"].get(key)
            if entry is None:
                return {
                    "ok_streak": 0,
                    "condemn_streak": 0,
                    "temp_streak": 0,
                    "last_bucket": None,
                    "last_tier": None,
                    "last_priority": None,
                    "dead_since": None,
                    "first_seen": None,
                }
            condemn = entry.get("condemn_streak")
            if condemn is None:  # migrate from legacy fail_streak
                condemn = entry.get("fail_streak", 0)
            return {
                "ok_streak": int(entry.get("ok_streak", 0)),
                "condemn_streak": int(condemn or 0),
                "temp_streak": int(entry.get("temp_streak", 0)),
                "last_bucket": entry.get("last_bucket"),
                "last_tier": entry.get("last_tier"),
                "last_priority": entry.get("last_priority"),
                "dead_since": entry.get("dead_since"),
                "first_seen": entry.get("first_seen"),
            }

    def record(self, key, *, decision, probe_bucket):
        """Persist the streak counters the scorer computed for this round.

        Thread-safe: called concurrently from the probe worker pool. This is a
        dumb store -- all counter logic lives in scorer.score() and arrives via
        the Decision, so the two can never drift out of sync.
        """
        with self._lock:
            prev = self._data["entries"].get(key) or {}
            now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())

            dead_since = prev.get("dead_since")
            if decision.tier == "dead" and not dead_since:
                dead_since = now
            elif decision.tier != "dead":
                dead_since = None

            self._data["entries"][key] = {
                "ok_streak": int(decision.ok_streak),
                "condemn_streak": int(decision.condemn_streak),
                "temp_streak": int(decision.temp_streak),
                "last_bucket": probe_bucket,
                "last_tier": decision.tier,
                "last_priority": decision.priority,
                "dead_since": dead_since,
                "first_seen": prev.get("first_seen") or now,
                "updated_at": now,
            }
            self._dirty = True

    def prune(self, live_keys):
        """Drop state for credentials no longer present in the config."""
        with self._lock:
            live = set(live_keys)
            existing = set(self._data["entries"].keys())
            removed = existing - live
            for key in removed:
                del self._data["entries"][key]
            if removed:
                self._dirty = True
            return len(removed)

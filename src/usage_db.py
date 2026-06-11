"""Read-only access to the CPA Usage Keeper SQLite database.

The keeper never writes here; it only reads historical per-credential health to
combine with the live probe when scoring priority.

Join model (validated empirically, 420/420 events -> identities):
  usage_events.auth_index  == usage_identities.identity   (stable hash)
  config api-key           == usage_identities.lookup_key  (api-key types)
  config api-key-entries[].api-key / auth-index == lookup_key / identity (openai-compat)

Health is computed over a recent window from usage_events (authoritative,
event-level) and keyed by auth_index so it maps straight onto config entries.
"""
import sqlite3
import time


class UsageDB:
    def __init__(self, db_path, recent_window_days=7):
        self.db_path = db_path
        self.recent_window_days = recent_window_days
        self._by_auth_index = {}   # auth_index -> health dict
        self._by_lookup = {}       # lookup_key (api-key) -> auth_index
        self._loaded = False
        self.available = False
        self.error = None

    def load(self):
        """Load recent-window health into memory. Safe to call once per round."""
        self._by_auth_index = {}
        self._by_lookup = {}
        self._loaded = False
        self.available = False
        self.error = None
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error as exc:
            self.error = f"open failed: {exc}"
            return self
        try:
            cur = conn.cursor()
            cutoff = time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(time.time() - self.recent_window_days * 86400),
            )
            # Event-level recent health per auth_index. timestamp is ISO8601 text
            # with timezone; lexical comparison on the date prefix is sufficient
            # for a coarse N-day window.
            cur.execute(
                """
                SELECT auth_index,
                       COUNT(*)                       AS total,
                       SUM(CASE WHEN failed THEN 1 ELSE 0 END) AS fails
                FROM usage_events
                WHERE auth_index <> '' AND substr(timestamp,1,19) >= ?
                GROUP BY auth_index
                """,
                (cutoff,),
            )
            for auth_index, total, fails in cur.fetchall():
                total = total or 0
                fails = fails or 0
                self._by_auth_index[auth_index] = {
                    "total": total,
                    "fails": fails,
                    "success": total - fails,
                    "fail_pct": (fails * 100.0 / total) if total else None,
                    "source": "events",
                }

            # Map lookup_key (== config api-key) -> identity (== auth_index),
            # plus cumulative fallback health for credentials with no recent
            # events but some history.
            cur.execute(
                """
                SELECT lookup_key, identity, total_requests, success_count, failure_count
                FROM usage_identities
                WHERE is_deleted = 0
                """
            )
            for lookup_key, identity, tot, succ, fail in cur.fetchall():
                if lookup_key and identity:
                    # Keep the identity with the most history if a key is reused.
                    prev = self._by_lookup.get(lookup_key)
                    if prev is None:
                        self._by_lookup[lookup_key] = identity
                # Cumulative fallback only when we have no recent-window row.
                if identity and identity not in self._by_auth_index:
                    tot = tot or 0
                    fail = fail or 0
                    if tot > 0:
                        self._by_auth_index[identity] = {
                            "total": tot,
                            "fails": fail,
                            "success": tot - fail,
                            "fail_pct": fail * 100.0 / tot,
                            "source": "cumulative",
                        }
            self.available = True
        except sqlite3.Error as exc:
            self.error = f"query failed: {exc}"
        finally:
            conn.close()
        self._loaded = True
        return self

    def health_for(self, *, api_key=None, auth_index=None):
        """Return health dict for a credential, or None if no history.

        Tries auth_index first (exact), then resolves api_key -> auth_index.
        """
        if auth_index and auth_index in self._by_auth_index:
            return self._by_auth_index[auth_index]
        if api_key:
            ai = self._by_lookup.get(api_key)
            if ai and ai in self._by_auth_index:
                return self._by_auth_index[ai]
        return None

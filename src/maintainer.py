"""CPAPriorityKeeper orchestrator.

One round:
  1. load recent-window health from the usage keeper DB (read-only)
  2. load persistent cross-round state (streak counters)
  3. for each managed credential type, GET the full entry list from the CPA
     management API
  4. for each entry: pick a routable probe model (prefix entries only), send a
     live request, classify it, combine with DB health + prior streaks -> a
     target priority / tier (+ a disabled flag for openai-compatibility)
  5. apply per TYPE with a single atomic PUT of the whole list

Why PUT, not PATCH: the CLIProxyAPI management PATCH handlers use typed structs
that whitelist editable fields, and `priority` is NOT among them (only
openai-compatibility's `disabled` is). PATCH therefore SILENTLY DROPS priority.
Only the PUT (full-list replace, decoded into the complete config struct) honors
`priority`. So we GET the list, mutate only priority/disabled on a verbatim copy
of every entry, and PUT the whole list back once per type. A GET->PUT round-trip
was verified lossless for all four types.

Concurrency: entries are probed in a thread pool; scoring and state writes are
guarded; the PUT writes happen sequentially after all scoring completes, so we
never race a half-scored list onto the server.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .cpa_client import CPAClient
from .logging_utils import ConsoleLogger, EntryLogger
from .prober import Prober, pick_probe_model, OK, PERM_FAIL, TEMP_FAIL, INCONCLUSIVE
from . import scorer
from .state import KeeperState
from .usage_db import UsageDB


# Credential types managed, in display order.
_CRED_TYPES = ("codex-api-key", "openai-compatibility", "gemini-api-key", "claude-api-key")


def _entry_keys(cred_type, entry):
    """Return (api_keys, auth_indexes) usable to look the entry up in the DB."""
    api_keys, auth_idx = [], []
    if cred_type == "openai-compatibility":
        for ke in entry.get("api-key-entries") or []:
            if ke.get("api-key"):
                api_keys.append(ke["api-key"])
            if ke.get("auth-index"):
                auth_idx.append(ke["auth-index"])
    else:
        if entry.get("api-key"):
            api_keys.append(entry["api-key"])
        if entry.get("auth-index"):
            auth_idx.append(entry["auth-index"])
    return api_keys, auth_idx


def _entry_label(cred_type, idx, entry):
    name = entry.get("name")
    prefix = entry.get("prefix")
    base = (entry.get("base-url") or "")[:40]
    tag = name or prefix or base or f"#{idx}"
    return f"{cred_type}#{idx} {tag}"


def _strip_runtime_fields(entry):
    """Return a copy safe to PUT back: drop server-injected read-only fields.

    `auth-index` is computed by the server from the credential content; it is
    echoed on GET but must not be sent back as a config field. We strip it from
    the entry and from each api-key-entry so the PUT body matches the on-disk
    schema exactly.
    """
    value = dict(entry)
    value.pop("auth-index", None)
    if "api-key-entries" in value and isinstance(value["api-key-entries"], list):
        cleaned = []
        for ke in value["api-key-entries"]:
            ke2 = dict(ke)
            ke2.pop("auth-index", None)
            cleaned.append(ke2)
        value["api-key-entries"] = cleaned
    return value


class CPAPriorityKeeper:
    def __init__(self, settings, dry_run=False):
        self.settings = settings
        self.dry_run = dry_run
        self.logger = ConsoleLogger()
        self.client = CPAClient(
            settings.cpa_endpoint, settings.cpa_token,
            proxy=None, timeout=settings.http_timeout_seconds,
            max_retries=settings.max_retries,
        )
        self.prober = Prober(
            settings.cpa_endpoint, settings.client_api_key,
            proxy=None, timeout=settings.probe_timeout_seconds,
            max_tokens=settings.probe_max_tokens, prompt=settings.probe_prompt,
        ) if settings.enable_live_probe else None
        self.db = UsageDB(settings.usage_db_path, settings.recent_window_days)
        self.state = KeeperState(settings.state_path)

    # ---- per-entry scoring (NO write; pure decision) ------------------------
    def _score_entry(self, cred_type, idx, entry, total, visible_models):
        """Score one entry. Returns a plan dict describing the desired state.

        plan = {
          idx, cred_type, label, outcome,
          target_priority, want_disabled, current_priority, current_disabled,
          changed (bool), pinned (bool),
        }
        Records the cross-round streak state as a side effect.
        """
        s = self.settings
        label = _entry_label(cred_type, idx, entry)
        log = EntryLogger(self.logger, label)
        log.header(idx, total)
        api_keys, auth_idxs = _entry_keys(cred_type, entry)
        state_key = self.state.key_for(cred_type, entry, auth_idxs, api_keys)
        current_prio = int(entry.get("priority") or 0)
        disabled_now = bool(entry.get("disabled", False))
        plan = {
            "idx": idx, "cred_type": cred_type, "label": label,
            "current_priority": current_prio, "current_disabled": disabled_now,
            "target_priority": current_prio, "want_disabled": disabled_now,
            "changed": False, "pinned": False, "outcome": "skip",
        }
        try:
            # Pinned: premium / manual overrides at/above the floor are untouched.
            if current_prio >= s.pin_floor:
                log.log("SKIP", f"pinned (priority {current_prio} >= {s.pin_floor})", indent=1)
                plan["pinned"] = True
                plan["outcome"] = "pinned"
                return plan

            # DB health (best of any matching key/auth-index).
            health = None
            for ai in auth_idxs:
                health = self.db.health_for(auth_index=ai)
                if health:
                    break
            if not health:
                for ak in api_keys:
                    health = self.db.health_for(api_key=ak)
                    if health:
                        break

            # Live probe (prefix-routable entries only).
            probe_bucket = None
            if self.prober is not None:
                route, _is_img = pick_probe_model(entry, visible_models)
                if route is None:
                    pooled = not bool((entry.get("prefix") or "").strip())
                    why = "pooled/no-prefix" if pooled else "image-only/excluded"
                    log.log("INFO", f"probe skipped ({why}) -> db-only", indent=1)
                else:
                    bucket, status, ms, brief = self.prober.probe(route)
                    probe_bucket = bucket
                    log.log("INFO", f"probe {route} -> {bucket} ({status}, {ms}ms) {brief}", indent=1)

            prev_state = self.state.get(state_key)
            decision = scorer.score(
                probe_bucket=probe_bucket, health=health, state=prev_state, settings=s,
            )
            self.state.record(state_key, decision=decision, probe_bucket=probe_bucket)

            db_str = (f"db {health['fail_pct']:.0f}% n={health['total']}"
                      if health and health.get("fail_pct") is not None else "db none")
            streak_str = (f"ok{prev_state.get('ok_streak', 0)}/"
                          f"condemn{prev_state.get('condemn_streak', 0)}/"
                          f"temp{prev_state.get('temp_streak', 0)}")
            log.log("INFO", f"current prio={current_prio} disabled={disabled_now} "
                            f"| {db_str} | prev {streak_str}", indent=1)

            # Decide target disabled flag (openai-compat only supports it).
            want_disabled = disabled_now
            if cred_type == "openai-compatibility" and s.enable_disable_dead:
                if disabled_now:
                    # Never auto-enable a manually-disabled entry without POSITIVE
                    # evidence it works again (live OK, or DB clearly succeeding).
                    db_ok = (
                        health is not None
                        and health.get("fail_pct") is not None
                        and health.get("total", 0) >= s.min_sample
                        and health["fail_pct"] < s.usable_threshold
                    )
                    if probe_bucket == OK or db_ok:
                        want_disabled = False
                    else:
                        want_disabled = True
                        log.log("INFO", "stays disabled (no positive evidence to re-enable)", indent=1)
                else:
                    want_disabled = bool(decision.mark_dead)

            plan["target_priority"] = decision.priority
            plan["want_disabled"] = want_disabled
            plan["outcome"] = decision.tier

            prio_changed = decision.priority != current_prio
            dis_changed = (cred_type == "openai-compatibility") and (want_disabled != disabled_now)
            plan["changed"] = prio_changed or dis_changed

            change = f"prio {current_prio}->{decision.priority}"
            if dis_changed:
                change += f", disabled {disabled_now}->{want_disabled}"
            if not plan["changed"]:
                log.log("OK", f"unchanged -> {decision.tier} (prio {decision.priority}) | {decision.reason}", indent=1)
            else:
                level = "DEAD" if decision.tier == "dead" else "SET"
                verb = "would set" if self.dry_run else "plan"
                log.log(level, f"{verb} {change} [{decision.tier}] | {decision.reason}", indent=1)
            return plan
        finally:
            log.flush()

    # ---- one round ----------------------------------------------------------
    def run_once(self):
        s = self.settings
        self.logger.divider()
        self.logger.log("INFO", "CPAPriorityKeeper round start")
        self.logger.log("INFO", f"endpoint={s.cpa_endpoint}  dry_run={self.dry_run}")
        self.logger.log("INFO", f"probe={'on' if self.prober else 'off'}  "
                                f"window={s.recent_window_days}d  min_sample={s.min_sample}")

        self.db.load()
        if self.db.available:
            self.logger.log("INFO", f"usage db loaded: {self.db.db_path}")
        else:
            self.logger.log("WARN", f"usage db unavailable ({self.db.error}) -> probe-only scoring")

        self.state.load()
        self.logger.log("INFO", f"state file: {self.state.path}")

        visible_models = self.client.list_models(s.client_api_key) if self.prober else set()
        if self.prober:
            self.logger.log("INFO", f"visible models: {len(visible_models)}")

        type_enabled = {
            "codex-api-key": s.manage_codex_key,
            "openai-compatibility": s.manage_openai_compat,
            "gemini-api-key": s.manage_gemini_key,
            "claude-api-key": s.manage_claude_key,
        }

        # GET each managed type ONCE and keep the verbatim entry list; we PUT a
        # mutated copy of this exact list back so nothing else is disturbed.
        entries_by_type = {}
        work = []
        for cred_type in _CRED_TYPES:
            if not type_enabled[cred_type]:
                continue
            entries = self.client.get_entries(cred_type)
            entries_by_type[cred_type] = entries
            for idx, entry in enumerate(entries):
                work.append((cred_type, idx, entry, len(entries)))
        self.logger.log("INFO", f"managing {len(work)} credential entries")
        self.logger.blank_line()

        # Score every entry concurrently (probes dominate wall-clock).
        plans = []
        with ThreadPoolExecutor(max_workers=s.worker_threads) as ex:
            futs = {
                ex.submit(self._score_entry, ct, idx, entry, total, visible_models): (ct, idx)
                for (ct, idx, entry, total) in work
            }
            for fut in as_completed(futs):
                try:
                    plans.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    ct, idx = futs[fut]
                    self.logger.log("ERROR", f"{ct}#{idx} raised: {exc}")

        # Apply per type with a single atomic PUT of the full (mutated) list.
        results = self._apply(entries_by_type, plans)

        # Drop state for credentials no longer in the config, then persist.
        live_keys = []
        for (ct, idx, entry, total) in work:
            ak, ai = _entry_keys(ct, entry)
            live_keys.append(self.state.key_for(ct, entry, ai, ak))
        pruned = self.state.prune(live_keys)
        if pruned:
            self.logger.log("INFO", f"pruned {pruned} stale state entries")
        if not self.dry_run:
            self.state.save()

        self._summarize(results)

    def _apply(self, entries_by_type, plans):
        """Mutate each type's entry list per its plans and PUT it back once.

        Returns a list of (outcome, cred_type) for the summary. The outcome of an
        entry that belongs to a type whose PUT failed is suffixed ":put-failed".
        """
        s = self.settings
        plans_by_type = {}
        for p in plans:
            plans_by_type.setdefault(p["cred_type"], {})[p["idx"]] = p

        results = []
        for cred_type, entries in entries_by_type.items():
            type_plans = plans_by_type.get(cred_type, {})
            type_changed = any(p["changed"] for p in type_plans.values())

            # Build the mutated PUT body from the verbatim GET list.
            new_list = []
            for idx, entry in enumerate(entries):
                value = _strip_runtime_fields(entry)
                p = type_plans.get(idx)
                if p and not p["pinned"]:
                    value["priority"] = p["target_priority"]
                    if cred_type == "openai-compatibility":
                        value["disabled"] = p["want_disabled"]
                new_list.append(value)

            # Record per-entry outcomes for the summary.
            def _tag(p, suffix=""):
                if p["pinned"]:
                    return ("pinned", cred_type)
                base = ("would:" if self.dry_run else ("set:" if p["changed"] else "")) + p["outcome"]
                if not p["changed"] and not self.dry_run:
                    base = p["outcome"]
                return (base + suffix, cred_type)

            if self.dry_run:
                for idx in sorted(type_plans):
                    results.append(_tag(type_plans[idx]))
                continue

            if not type_changed:
                for idx in sorted(type_plans):
                    results.append(_tag(type_plans[idx]))
                continue

            ok, detail = self.client.put_entries(cred_type, new_list)
            if ok:
                self.logger.log("SET", f"PUT {cred_type}: applied {len(new_list)} entries", indent=0)
                for idx in sorted(type_plans):
                    results.append(_tag(type_plans[idx]))
            else:
                self.logger.log("ERROR", f"PUT {cred_type} FAILED ({detail}); no change applied for this type", indent=0)
                for idx in sorted(type_plans):
                    results.append(_tag(type_plans[idx], suffix=":put-failed"))
        return results

    def _summarize(self, results):
        tally = {}
        for outcome, _ct in results:
            tally[outcome] = tally.get(outcome, 0) + 1
        self.logger.blank_line()
        self.logger.divider()
        self.logger.log("INFO", "round complete")
        for key in sorted(tally):
            self.logger.log("INFO", f"- {key}: {tally[key]}", indent=1)
        self.logger.divider()

    def run_forever(self, interval_seconds):
        round_no = 0
        self.logger.log("INFO", f"daemon mode, interval={interval_seconds}s")
        while True:
            round_no += 1
            self.logger.log("INFO", f"=== round {round_no} ===")
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.log("ERROR", f"round {round_no} failed: {exc}")
            self.logger.log("INFO", f"sleeping {interval_seconds}s")
            time.sleep(interval_seconds)

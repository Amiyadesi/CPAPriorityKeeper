"""Combine live-probe outcome + usage-DB history + cross-round state into a
target priority tier.

Priority direction (verified against CLIProxyAPI sdk/cliproxy/auth/selector.go):
  getAvailableAuths picks the MAX priority tier, default/missing == 0 == lowest.
  So HIGHER number == served first under fill-first.

Probe buckets (from prober.classify):
  OK            real assistant text          -> strong positive (works right now)
  TEMP_FAIL     quota / rate-limit / balance -> recoverable; rest LOW, not dead
  PERM_FAIL     revoked / invalid token      -> permanent; can go dead
  INCONCLUSIVE  routing artifact / timeout   -> ignore probe, use DB only

Guiding rules learned from the data:
  * A single probe FAILURE must never kill a credential the DB shows healthy.
  * A single probe OK must not crown a credential the DB shows mostly-failing:
    when there is enough history we rank by DB fail% and only use the OK as a
    floor (an endpoint that just served us is never buried as dead/poor).
  * A quota exhaustion (TEMP_FAIL) is NOT death: park it at the "resting" tier so
    it is tried last, then let it climb straight back when credit returns.
  * Death needs CONFIRMATION: a unified `condemn_streak` increments every round
    the evidence says dead-worthy (from PERM_FAIL probe OR DB ~100%). Only after
    `dead_streak` consecutive condemned rounds does a credential actually go dead
    (and openai-compat get disabled). Any OK / TEMP_FAIL / DB-healthy round
    resets the streak, so recovery is automatic.

`min_sample` guards DB fail% from being trusted on 1-2 events.

The scorer OWNS all streak transitions and returns them in the Decision so the
persistent state is a dumb store (no duplicated counter logic that could drift
out of sync with the scoring rules).
"""
from .prober import OK, PERM_FAIL, TEMP_FAIL, INCONCLUSIVE


class Decision:
    """Result of scoring one credential for one round."""

    __slots__ = (
        "priority", "tier", "mark_dead", "reason",
        "ok_streak", "condemn_streak", "temp_streak",
    )

    def __init__(self, priority, tier, mark_dead, reason,
                 ok_streak, condemn_streak, temp_streak):
        self.priority = priority
        self.tier = tier
        self.mark_dead = mark_dead
        self.reason = reason
        self.ok_streak = ok_streak
        self.condemn_streak = condemn_streak
        self.temp_streak = temp_streak


def _tier_from_failpct(fail_pct, s):
    """Map a fail percentage to a (priority, tier-name) using settings thresholds."""
    if fail_pct < s.healthy_threshold:
        return s.prio_healthy, "healthy"
    if fail_pct < s.good_threshold:
        return s.prio_good, "good"
    if fail_pct < s.usable_threshold:
        return s.prio_usable, "usable"
    if fail_pct < s.flaky_threshold:
        return s.prio_flaky, "flaky"
    if fail_pct < s.dead_threshold:
        return s.prio_poor, "poor"
    return s.prio_dead, "dead"


def score(*, probe_bucket, health, state, settings):
    """Return a Decision combining probe + DB history + cross-round streaks.

    probe_bucket: OK / TEMP_FAIL / PERM_FAIL / INCONCLUSIVE / None (no probe)
    health: dict from UsageDB.health_for(), or None when there is no history.
    state: dict from KeeperState.get() with prior streak counters (never None).
    """
    s = settings
    has_db = health is not None and health.get("total", 0) > 0
    enough = has_db and health["total"] >= s.min_sample
    fail_pct = health["fail_pct"] if has_db else None
    samples = health["total"] if has_db else 0
    db_healthy = enough and fail_pct < s.usable_threshold

    prev_ok = state.get("ok_streak", 0) or 0
    prev_condemn = state.get("condemn_streak", 0) or 0
    prev_temp = state.get("temp_streak", 0) or 0
    prev_priority = state.get("last_priority")
    was_dead = state.get("last_tier") == "dead"

    # ---- probe OK: works right now -----------------------------------------
    if probe_bucket == OK:
        ok_streak = prev_ok + 1
        # Recovery confirmation: a credential we previously condemned to "dead"
        # must probe OK `promote_streak` consecutive times before we lift it back
        # above "resting" (default 1 = lift immediately). Holds it just above dead
        # in the meantime so a single anti-bot fluke that returns 200 once cannot
        # instantly re-promote a truly broken endpoint.
        if was_dead and ok_streak < s.promote_streak:
            return Decision(
                s.prio_resting, "resting(recovering)", False,
                f"probe OK, confirming recovery ok_streak={ok_streak}/{s.promote_streak}",
                ok_streak, 0, 0,
            )
        if enough:
            prio, tier = _tier_from_failpct(fail_pct, s)
            # Floor: an endpoint that just served a real request is never ranked
            # below "flaky", regardless of rough history.
            if prio < s.prio_flaky:
                prio, tier = s.prio_flaky, "flaky(probe-ok)"
            reason = f"probe OK, db fail%={fail_pct:.0f} n={samples}"
        else:
            prio, tier = s.prio_good, "good(probe-ok)"
            reason = "probe OK, little/no history"
        return Decision(prio, tier, False, reason, ok_streak, 0, 0)

    # ---- probe TEMP_FAIL: temporarily out of budget -> rest, never dead -----
    if probe_bucket == TEMP_FAIL:
        temp_streak = prev_temp + 1
        if db_healthy:
            reason = (f"probe TEMP_FAIL (quota), db fail%={fail_pct:.0f} "
                      f"n={samples} -> rest")
        else:
            reason = "probe TEMP_FAIL (quota/rate-limit) -> rest"
        return Decision(s.prio_resting, "resting", False, reason, 0, 0, temp_streak)

    # ---- probe PERM_FAIL: revoked/invalid -> condemn (with DB override) -----
    if probe_bucket == PERM_FAIL:
        # DB strongly disagrees (healthy over enough samples): trust the larger
        # sample, demote to poor but do NOT condemn -- likely a transient block.
        if db_healthy:
            prio, tier = _tier_from_failpct(fail_pct, s)
            if prio > s.prio_poor:
                prio, tier = s.prio_poor, "poor(db-overrides-probe)"
            return Decision(prio, tier, False,
                            f"probe PERM_FAIL but db fail%={fail_pct:.0f} n={samples} -> keep low",
                            0, 0, 0)
        return _condemn(prev_condemn, samples, fail_pct, enough, s,
                        source="probe PERM_FAIL")

    # ---- no usable probe signal (INCONCLUSIVE / pooled / skipped): DB only --
    if enough:
        if fail_pct >= s.dead_threshold:
            return _condemn(prev_condemn, samples, fail_pct, enough, s,
                            source="db ~dead")
        prio, tier = _tier_from_failpct(fail_pct, s)
        return Decision(prio, f"{tier}(db)", False,
                        f"db-only fail%={fail_pct:.0f} n={samples}", 0, 0, 0)
    if has_db:
        return Decision(s.prio_usable, "usable(thin-db)", False,
                        f"db-only thin n={samples}", 0, 0, 0)

    # Nothing at all: leave mid-low so it can earn its way up next round.
    return Decision(s.prio_usable, "usable(unknown)", False,
                    "no probe, no history", 0, 0, 0)


def _condemn(prev_condemn, samples, fail_pct, enough, s, *, source):
    """Advance the unified condemn streak; only kill after `dead_streak` rounds."""
    condemn_streak = prev_condemn + 1
    db_note = f" + db fail%={fail_pct:.0f} n={samples}" if enough else " (no history)"
    if condemn_streak >= s.dead_streak:
        return Decision(s.prio_dead, "dead", True,
                        f"{source} x{condemn_streak}>={s.dead_streak}{db_note}",
                        0, condemn_streak, 0)
    # Awaiting confirmation: hold at poor, do not disable yet.
    return Decision(s.prio_poor, "poor(condemn)", False,
                    f"{source} streak={condemn_streak}/{s.dead_streak}{db_note}",
                    0, condemn_streak, 0)

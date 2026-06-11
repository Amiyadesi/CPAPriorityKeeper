"""Unit tests for the scorer + prober classification — the core decision logic.

Run: python -m unittest discover -s tests
No third-party deps; pure stdlib unittest.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import scorer
from src.prober import classify, OK, PERM_FAIL, TEMP_FAIL, INCONCLUSIVE
from src.settings import Settings


def _settings(**over):
    base = dict(
        cpa_endpoint="http://x", cpa_token="t", usage_db_path="", client_api_key="k",
    )
    base.update(over)
    return Settings(**base)


def _state(**over):
    base = {
        "ok_streak": 0, "condemn_streak": 0, "temp_streak": 0,
        "last_bucket": None, "last_tier": None, "last_priority": None,
        "dead_since": None, "first_seen": None,
    }
    base.update(over)
    return base


def _health(total, fail_pct):
    return {"total": total, "fails": int(total * fail_pct / 100),
            "success": total - int(total * fail_pct / 100),
            "fail_pct": fail_pct, "source": "events"}


class TestClassify(unittest.TestCase):
    def test_real_content_is_ok(self):
        bucket, _ = classify(200, "", "import zipfile\n...")
        self.assertEqual(bucket, OK)

    def test_invalid_token_is_perm(self):
        bucket, _ = classify(401, '{"message":"Invalid token"}', "")
        self.assertEqual(bucket, PERM_FAIL)

    def test_quota_is_temp(self):
        bucket, _ = classify(403, '{"message":"用户额度不足, 剩余额度: 0"}', "")
        self.assertEqual(bucket, TEMP_FAIL)

    def test_rate_limit_is_temp(self):
        bucket, _ = classify(429, "Too Many Requests", "")
        self.assertEqual(bucket, TEMP_FAIL)

    def test_unknown_provider_is_inconclusive(self):
        bucket, _ = classify(502, '{"message":"unknown provider for model x"}', "")
        self.assertEqual(bucket, INCONCLUSIVE)

    def test_model_not_found_is_inconclusive(self):
        bucket, _ = classify(503, '{"code":"model_not_found"}', "")
        self.assertEqual(bucket, INCONCLUSIVE)

    def test_cloudflare_block_is_perm(self):
        bucket, _ = classify(403, "<!doctype html>...cloudflare...", "")
        self.assertEqual(bucket, PERM_FAIL)

    def test_5xx_no_marker_is_inconclusive(self):
        bucket, _ = classify(500, "internal error", "")
        self.assertEqual(bucket, INCONCLUSIVE)


class TestScorer(unittest.TestCase):
    def test_probe_ok_floors_at_flaky_even_with_bad_db(self):
        s = _settings()
        d = scorer.score(probe_bucket=OK, health=_health(50, 96), state=_state(), settings=s)
        # 96% fail would be "dead" by DB, but a live OK floors it at flaky.
        self.assertGreaterEqual(d.priority, s.prio_flaky)
        self.assertFalse(d.mark_dead)

    def test_probe_ok_healthy_db_is_healthy(self):
        s = _settings()
        d = scorer.score(probe_bucket=OK, health=_health(200, 5), state=_state(), settings=s)
        self.assertEqual(d.priority, s.prio_healthy)

    def test_perm_fail_but_db_healthy_keeps_low_not_dead(self):
        s = _settings()
        d = scorer.score(probe_bucket=PERM_FAIL, health=_health(500, 20),
                         state=_state(), settings=s)
        self.assertFalse(d.mark_dead)
        self.assertLessEqual(d.priority, s.prio_poor)

    def test_perm_fail_needs_streak_before_dead(self):
        s = _settings(dead_streak=2)
        # First strike: poor, not dead.
        d1 = scorer.score(probe_bucket=PERM_FAIL, health=_health(30, 100),
                          state=_state(condemn_streak=0), settings=s)
        self.assertFalse(d1.mark_dead)
        self.assertEqual(d1.condemn_streak, 1)
        # Second strike: dead.
        d2 = scorer.score(probe_bucket=PERM_FAIL, health=_health(30, 100),
                          state=_state(condemn_streak=1), settings=s)
        self.assertTrue(d2.mark_dead)
        self.assertEqual(d2.priority, s.prio_dead)

    def test_temp_fail_rests_never_dies(self):
        s = _settings()
        d = scorer.score(probe_bucket=TEMP_FAIL, health=_health(100, 10),
                         state=_state(), settings=s)
        self.assertEqual(d.priority, s.prio_resting)
        self.assertFalse(d.mark_dead)
        self.assertEqual(d.condemn_streak, 0)  # quota exhaustion never condemns

    def test_db_dead_via_inconclusive_advances_condemn_streak(self):
        # This is the regression that motivated the unified counter: an
        # INCONCLUSIVE probe + DB ~100% must still march toward dead.
        s = _settings(dead_streak=2)
        d1 = scorer.score(probe_bucket=INCONCLUSIVE, health=_health(20, 100),
                          state=_state(condemn_streak=0), settings=s)
        self.assertFalse(d1.mark_dead)
        self.assertEqual(d1.condemn_streak, 1)
        d2 = scorer.score(probe_bucket=INCONCLUSIVE, health=_health(20, 100),
                          state=_state(condemn_streak=1), settings=s)
        self.assertTrue(d2.mark_dead)

    def test_recovery_from_dead_rests_then_climbs(self):
        s = _settings(promote_streak=2)
        # Previously dead, now probes OK once: must hold at resting (confirming).
        d = scorer.score(probe_bucket=OK, health=_health(100, 5),
                         state=_state(last_tier="dead", ok_streak=0), settings=s)
        self.assertEqual(d.priority, s.prio_resting)
        self.assertEqual(d.ok_streak, 1)

    def test_recovery_confirmed_promotes(self):
        s = _settings(promote_streak=2)
        d = scorer.score(probe_bucket=OK, health=_health(100, 5),
                         state=_state(last_tier="dead", ok_streak=1), settings=s)
        self.assertGreater(d.priority, s.prio_resting)

    def test_no_signal_is_neutral(self):
        s = _settings()
        d = scorer.score(probe_bucket=None, health=None, state=_state(), settings=s)
        self.assertEqual(d.priority, s.prio_usable)
        self.assertFalse(d.mark_dead)

    def test_ok_resets_condemn_streak(self):
        s = _settings()
        d = scorer.score(probe_bucket=OK, health=_health(100, 10),
                         state=_state(condemn_streak=1), settings=s)
        self.assertEqual(d.condemn_streak, 0)


if __name__ == "__main__":
    unittest.main()

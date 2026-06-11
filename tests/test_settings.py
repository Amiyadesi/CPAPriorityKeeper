"""Tests for .env loading + validation."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.settings import load_settings, SettingsError  # noqa: E402


def _write_env(text):
    fd, path = tempfile.mkstemp(suffix=".env")
    os.close(fd)
    Path(path).write_text(text, encoding="utf-8")
    return Path(path)


class TestSettings(unittest.TestCase):
    def setUp(self):
        # Ensure ambient env vars don't leak into the file-based tests.
        for k in list(os.environ):
            if k.startswith("CPA_"):
                del os.environ[k]

    def test_minimal_valid(self):
        env = _write_env(
            "CPA_ENDPOINT=http://127.0.0.1:8317\n"
            "CPA_TOKEN=tok\n"
            "CPA_CLIENT_API_KEY=sk-x\n"
        )
        s = load_settings(env)
        self.assertEqual(s.cpa_endpoint, "http://127.0.0.1:8317")
        self.assertEqual(s.cpa_token, "tok")
        self.assertTrue(s.usage_db_path)   # auto-resolved
        self.assertTrue(s.state_path)      # auto-resolved
        env.unlink()

    def test_missing_endpoint_raises(self):
        env = _write_env("CPA_TOKEN=tok\nCPA_CLIENT_API_KEY=sk-x\n")
        with self.assertRaises(SettingsError):
            load_settings(env)
        env.unlink()

    def test_bad_endpoint_scheme_raises(self):
        env = _write_env(
            "CPA_ENDPOINT=127.0.0.1:8317\nCPA_TOKEN=tok\nCPA_CLIENT_API_KEY=sk-x\n"
        )
        with self.assertRaises(SettingsError):
            load_settings(env)
        env.unlink()

    def test_probe_requires_client_key(self):
        env = _write_env(
            "CPA_ENDPOINT=http://127.0.0.1:8317\nCPA_TOKEN=tok\n"
            "CPA_ENABLE_LIVE_PROBE=true\n"
        )
        with self.assertRaises(SettingsError):
            load_settings(env)
        env.unlink()

    def test_probe_off_allows_no_client_key(self):
        env = _write_env(
            "CPA_ENDPOINT=http://127.0.0.1:8317\nCPA_TOKEN=tok\n"
            "CPA_ENABLE_LIVE_PROBE=false\n"
        )
        s = load_settings(env)
        self.assertFalse(s.enable_live_probe)
        env.unlink()

    def test_bad_int_raises(self):
        env = _write_env(
            "CPA_ENDPOINT=http://127.0.0.1:8317\nCPA_TOKEN=tok\n"
            "CPA_CLIENT_API_KEY=sk-x\nCPA_INTERVAL=notanumber\n"
        )
        with self.assertRaises(SettingsError):
            load_settings(env)
        env.unlink()


if __name__ == "__main__":
    unittest.main()

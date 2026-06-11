import os
from dataclasses import dataclass, field
from pathlib import Path


# ---- Defaults --------------------------------------------------------------
DEFAULT_INTERVAL_SECONDS = 3600          # one full re-score per hour
DEFAULT_HTTP_TIMEOUT_SECONDS = 30        # CPA management API
DEFAULT_PROBE_TIMEOUT_SECONDS = 60       # live chat probe (codex can be slow)
DEFAULT_MAX_RETRIES = 2
DEFAULT_WORKER_THREADS = 6               # concurrent live probes
DEFAULT_PROBE_MAX_TOKENS = 64

# Health windows (days) used when reading the usage keeper DB.
DEFAULT_RECENT_WINDOW_DAYS = 7
# A credential needs at least this many recent requests before its DB fail%
# is trusted on its own; below this we lean on the live probe.
DEFAULT_MIN_SAMPLE = 8

# ---- Priority ladder (HIGHER = served first under fill-first) --------------
# Confirmed against CLIProxyAPI sdk/cliproxy/auth/selector.go:getAvailableAuths
# -> it selects the MAX priority tier; default/missing priority == 0 == lowest.
DEFAULT_PRIO_HEALTHY = 600      # probe OK and fail% < healthy_threshold
DEFAULT_PRIO_GOOD = 500         # probe OK and fail% < good_threshold
DEFAULT_PRIO_USABLE = 400       # probe OK and fail% < usable_threshold
DEFAULT_PRIO_FLAKY = 300        # fail% in [usable, flaky_threshold)
DEFAULT_PRIO_POOR = 200         # probe hard-fail now but historically worked
DEFAULT_PRIO_RESTING = 150      # quota/rate-limit exhausted now; recovers later
DEFAULT_PRIO_DEAD = 1           # permanently broken (revoked); last resort

# ---- Recovery / anti-flap --------------------------------------------------
# Consecutive permanent-failure signals required before a credential is demoted
# to "dead" (and openai-compat disabled). Damps single-round false negatives.
DEFAULT_DEAD_STREAK = 2
# Consecutive OK probes required before a previously-bad credential is fully
# promoted back to its health tier (otherwise it climbs one step at a time).
DEFAULT_PROMOTE_STREAK = 1
# A credential marked dead is still re-probed every round so it can recover when
# quota/credit returns. This flag lets a dead entry climb straight back on the
# first OK rather than waiting out the promote streak.
DEFAULT_FAST_RECOVERY = True

# Fail% thresholds (percent).
DEFAULT_HEALTHY_THRESHOLD = 15
DEFAULT_GOOD_THRESHOLD = 30
DEFAULT_USABLE_THRESHOLD = 50
DEFAULT_FLAKY_THRESHOLD = 75
# At or above this fail% (with enough samples) a credential is treated as dead.
DEFAULT_DEAD_THRESHOLD = 95

# Priorities at or above this are treated as pinned (premium OAuth / manual
# overrides). The keeper never rewrites them.
DEFAULT_PIN_FLOOR = 5000

PROJECT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class SettingsError(ValueError):
    pass


@dataclass(slots=True)
class Settings:
    cpa_endpoint: str
    cpa_token: str
    usage_db_path: str
    client_api_key: str
    proxy: str | None = None
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    http_timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS
    probe_timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    worker_threads: int = DEFAULT_WORKER_THREADS
    probe_max_tokens: int = DEFAULT_PROBE_MAX_TOKENS
    recent_window_days: int = DEFAULT_RECENT_WINDOW_DAYS
    min_sample: int = DEFAULT_MIN_SAMPLE
    probe_prompt: str = "写一个解压zip文件的python脚本，只要核心代码"

    enable_live_probe: bool = True
    # When true, also set the openai-compatibility "disabled" flag for dead providers.
    enable_disable_dead: bool = True
    # Manage these credential types.
    manage_codex_key: bool = True
    manage_openai_compat: bool = True
    manage_gemini_key: bool = True
    manage_claude_key: bool = True

    pin_floor: int = DEFAULT_PIN_FLOOR

    prio_healthy: int = DEFAULT_PRIO_HEALTHY
    prio_good: int = DEFAULT_PRIO_GOOD
    prio_usable: int = DEFAULT_PRIO_USABLE
    prio_flaky: int = DEFAULT_PRIO_FLAKY
    prio_poor: int = DEFAULT_PRIO_POOR
    prio_resting: int = DEFAULT_PRIO_RESTING
    prio_dead: int = DEFAULT_PRIO_DEAD

    healthy_threshold: int = DEFAULT_HEALTHY_THRESHOLD
    good_threshold: int = DEFAULT_GOOD_THRESHOLD
    usable_threshold: int = DEFAULT_USABLE_THRESHOLD
    flaky_threshold: int = DEFAULT_FLAKY_THRESHOLD
    dead_threshold: int = DEFAULT_DEAD_THRESHOLD

    # Recovery / anti-flap.
    dead_streak: int = DEFAULT_DEAD_STREAK
    promote_streak: int = DEFAULT_PROMOTE_STREAK
    fast_recovery: bool = DEFAULT_FAST_RECOVERY
    # Path to the persistent per-credential state file (JSON). Empty = sibling
    # of this package: CPAPriorityKeeper/state.json.
    state_path: str = ""


def _read_project_env_file(env_file: Path | None = None) -> dict[str, str]:
    target = env_file or PROJECT_ENV_FILE
    if not target.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in target.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _get(name: str, env_values: dict[str, str]) -> str | None:
    env_value = os.getenv(name)
    if env_value not in (None, ""):
        return env_value
    return env_values.get(name)


def _read_int(name, default, env_values, *, minimum=0, maximum=None):
    raw = _get(name, env_values)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if value < minimum:
        raise SettingsError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{name} must be <= {maximum}")
    return value


def _read_bool(name, default, env_values):
    raw = _get(name, env_values)
    if raw in (None, ""):
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SettingsError(f"{name} must be a boolean")


def load_settings(env_file: Path | None = None) -> Settings:
    env_values = _read_project_env_file(env_file)
    endpoint = (_get("CPA_ENDPOINT", env_values) or "").strip().rstrip("/")
    token = (_get("CPA_TOKEN", env_values) or "").strip()
    client_key = (_get("CPA_CLIENT_API_KEY", env_values) or "").strip()
    proxy = (_get("CPA_PROXY", env_values) or "").strip() or None

    if not endpoint:
        raise SettingsError("CPA_ENDPOINT is required")
    if not token:
        raise SettingsError("CPA_TOKEN is required")
    if not endpoint.startswith(("http://", "https://")):
        raise SettingsError("CPA_ENDPOINT must start with http:// or https://")

    enable_probe = _read_bool("CPA_ENABLE_LIVE_PROBE", True, env_values)
    if enable_probe and not client_key:
        raise SettingsError(
            "CPA_CLIENT_API_KEY is required when CPA_ENABLE_LIVE_PROBE is true "
            "(it must be one of the keys under api-keys: in config.yaml)"
        )

    # Default DB path: sibling cpa-usage-keeper package.
    default_db = (
        Path(__file__).resolve().parents[2]
        / "cpa-usage-keeper_v1.8.1_windows_amd64" / "data" / "app.db"
    )
    db_path = (_get("CPA_USAGE_DB", env_values) or "").strip() or str(default_db)

    # Default state file: alongside this package (CPAPriorityKeeper/state.json).
    default_state = Path(__file__).resolve().parents[1] / "state.json"
    state_path = (_get("CPA_STATE_PATH", env_values) or "").strip() or str(default_state)

    return Settings(
        cpa_endpoint=endpoint,
        cpa_token=token,
        usage_db_path=db_path,
        client_api_key=client_key,
        proxy=proxy,
        interval_seconds=_read_int("CPA_INTERVAL", DEFAULT_INTERVAL_SECONDS, env_values, minimum=60),
        http_timeout_seconds=_read_int("CPA_HTTP_TIMEOUT", DEFAULT_HTTP_TIMEOUT_SECONDS, env_values, minimum=1),
        probe_timeout_seconds=_read_int("CPA_PROBE_TIMEOUT", DEFAULT_PROBE_TIMEOUT_SECONDS, env_values, minimum=1),
        max_retries=_read_int("CPA_MAX_RETRIES", DEFAULT_MAX_RETRIES, env_values, minimum=0, maximum=5),
        worker_threads=_read_int("CPA_WORKER_THREADS", DEFAULT_WORKER_THREADS, env_values, minimum=1, maximum=32),
        probe_max_tokens=_read_int("CPA_PROBE_MAX_TOKENS", DEFAULT_PROBE_MAX_TOKENS, env_values, minimum=8),
        recent_window_days=_read_int("CPA_RECENT_WINDOW_DAYS", DEFAULT_RECENT_WINDOW_DAYS, env_values, minimum=1),
        min_sample=_read_int("CPA_MIN_SAMPLE", DEFAULT_MIN_SAMPLE, env_values, minimum=1),
        probe_prompt=(_get("CPA_PROBE_PROMPT", env_values) or "写一个解压zip文件的python脚本，只要核心代码").strip(),
        enable_live_probe=enable_probe,
        enable_disable_dead=_read_bool("CPA_DISABLE_DEAD", True, env_values),
        manage_codex_key=_read_bool("CPA_MANAGE_CODEX_KEY", True, env_values),
        manage_openai_compat=_read_bool("CPA_MANAGE_OPENAI_COMPAT", True, env_values),
        manage_gemini_key=_read_bool("CPA_MANAGE_GEMINI_KEY", True, env_values),
        manage_claude_key=_read_bool("CPA_MANAGE_CLAUDE_KEY", True, env_values),
        pin_floor=_read_int("CPA_PIN_FLOOR", DEFAULT_PIN_FLOOR, env_values, minimum=1),
        prio_healthy=_read_int("CPA_PRIO_HEALTHY", DEFAULT_PRIO_HEALTHY, env_values, minimum=1),
        prio_good=_read_int("CPA_PRIO_GOOD", DEFAULT_PRIO_GOOD, env_values, minimum=1),
        prio_usable=_read_int("CPA_PRIO_USABLE", DEFAULT_PRIO_USABLE, env_values, minimum=1),
        prio_flaky=_read_int("CPA_PRIO_FLAKY", DEFAULT_PRIO_FLAKY, env_values, minimum=1),
        prio_poor=_read_int("CPA_PRIO_POOR", DEFAULT_PRIO_POOR, env_values, minimum=1),
        prio_resting=_read_int("CPA_PRIO_RESTING", DEFAULT_PRIO_RESTING, env_values, minimum=1),
        prio_dead=_read_int("CPA_PRIO_DEAD", DEFAULT_PRIO_DEAD, env_values, minimum=1),
        healthy_threshold=_read_int("CPA_HEALTHY_THRESHOLD", DEFAULT_HEALTHY_THRESHOLD, env_values, minimum=0, maximum=100),
        good_threshold=_read_int("CPA_GOOD_THRESHOLD", DEFAULT_GOOD_THRESHOLD, env_values, minimum=0, maximum=100),
        usable_threshold=_read_int("CPA_USABLE_THRESHOLD", DEFAULT_USABLE_THRESHOLD, env_values, minimum=0, maximum=100),
        flaky_threshold=_read_int("CPA_FLAKY_THRESHOLD", DEFAULT_FLAKY_THRESHOLD, env_values, minimum=0, maximum=100),
        dead_threshold=_read_int("CPA_DEAD_THRESHOLD", DEFAULT_DEAD_THRESHOLD, env_values, minimum=0, maximum=100),
        dead_streak=_read_int("CPA_DEAD_STREAK", DEFAULT_DEAD_STREAK, env_values, minimum=1, maximum=20),
        promote_streak=_read_int("CPA_PROMOTE_STREAK", DEFAULT_PROMOTE_STREAK, env_values, minimum=1, maximum=20),
        fast_recovery=_read_bool("CPA_FAST_RECOVERY", DEFAULT_FAST_RECOVERY, env_values),
        state_path=state_path,
    )

# CPAPriorityKeeper

[中文](README.md) | [English](README.en.md)

CPAPriorityKeeper is a background tool that **automatically tunes the priority and enabled-state of CPA relay / API-key providers**. It is a sister project to [CPACodexKeeper](https://github.com/Amiyadesi/CPACodexKeeper).

- **CPACodexKeeper** manages codex **OAuth accounts** (auth-files): deletes dead ones, disables/enables by quota, refreshes near-expiry tokens.
- **CPAPriorityKeeper** (this project) manages **api-key / relay providers** (that big pile of `priority: 1000` in your config): it re-ranks them by real health, sinks dead relays to the bottom, and lets recovered relays automatically climb back up.

The two are complementary — they write different credential types and never fight over the same one.

> **Default is "passive mode"**: it re-ranks priority purely from the Usage Keeper's historical success/failure rates, sending **zero extra probe requests — no quota burned**. More failures → lower priority; successes → higher priority, fully automatic. A dead relay only sinks to priority `1` (kept as fallback) and is **not disabled by default** — because once disabled it gets no traffic, so the DB can never see it recover (a one-way trap). Left at `1` it still catches occasional fallback traffic, so when its budget returns the DB sees the successes and lifts it back automatically.
>
> For more aggressive real-time liveness checks, enable `CPA_ENABLE_LIVE_PROBE=true` in `.env` (sends a real request to every routable provider, burning quota) and `CPA_DISABLE_DEAD=true` (additionally sets `disabled` on confirmed-dead openai-compat entries).

> This project targets **authorized local / internal maintenance**: your own CPA instance, your own relay account pool. Do not use it against targets you are not authorized to manage.

## What problem it solves

A CPA `config.yaml` typically has dozens of `codex-api-key` / `openai-compatibility` / `gemini-api-key` / `claude-api-key` providers, almost all stacked at `priority: 1000` — i.e. flat in routing, with order effectively random. Worse, some **long-dead** relays were manually set to `10000`, so under the `fill-first` strategy they get used **first**, causing mass request failures.

But relay availability is **dynamic**:

- Out of quota this cycle → keeps failing; quota resets next cycle → works again.
- Token revoked / account banned → permanently dead.
- Upstream Cloudflare / routing jitter → occasional failures, but the credential itself is fine.

CPAPriorityKeeper's goal is to **tell these cases apart**: rank the genuinely-working ones first, sink the dead ones to priority `1`, and automatically bring them back to the front of the queue once the DB sees them recover.

## What each round does

1. Read **recent N-day real success/failure rates per credential** from the [CPA Usage Keeper](https://github.com/Willxup/cpa-usage-keeper) SQLite DB (read-only, joined to config entries by `auth_index` / `lookup_key`).
2. Combine "historical health" + "cross-round state" into a target priority tier (lower fail% → higher tier).
3. Only when priority (or openai-compatibility's `disabled`) actually changes, write it back atomically via the CPA management API using a **full-list PUT**.

> **Optional probe (off by default)**: with `CPA_ENABLE_LIVE_PROBE=true`, each round additionally sends a **real request** (default prompt: "write a python script to unzip a file, core code only") to every **prefix-routable** provider and folds the live result into scoring. This burns quota — enable it only when you want active liveness checks.

## Priority direction (confirmed against source)

CLIProxyAPI's `sdk/cliproxy/auth/selector.go:getAvailableAuths` selects the **maximum** priority tier; missing/unset == `0` == lowest.

> **Higher number = used first.** The worst relays get `1` (kept as a last-resort fallback, never deleted).

## Why PUT, not PATCH

Verified empirically: the CPA management API's `PATCH /v0/management/<type>` handler uses a **field-whitelist** struct per credential type, and `priority` is **not in the whitelist** — it is **silently dropped** (only openai-compatibility's `disabled` is whitelisted, so that one applies). Therefore the **only** reliable way to set `priority` is to `PUT` the whole list — PUT decodes the body into the full entry struct (which includes `priority`).

So this project does: `GET` the full list → score each entry → mutate only `priority`/`disabled` on a **verbatim copy** → `PUT` the full list. All other fields and ordering are preserved.

## Four-state probe classification (only with probing on)

> Passive mode has no probe — skip this section. Only when `CPA_ENABLE_LIVE_PROBE=true`, `prober.classify()` maps a probe to four semantics — the key to "temporary vs permanent failure":

| Bucket | Meaning | Triggers | Handling |
|--------|---------|----------|----------|
| `OK` | works right now | got real answer text | strong positive |
| `TEMP_FAIL` | temporarily out of budget, self-recovers | quota/rate-limit/balance: `429`, `402`, `quota`, `rate limit`, `balance`, `额度不足`… | sink to `resting`, **never killed**, climbs back immediately once it probes OK |
| `PERM_FAIL` | permanently dead | revoked/invalid/banned/Cloudflare: `401`, `403`, `invalid token`, `revoked`, `forbidden`… | counts toward the condemn streak; only killed at threshold |
| `INCONCLUSIVE` | routing jitter/timeout, not the credential's fault | `unknown provider`, `model_not_found`, `5xx`, timeout; or unattributable pooled entries | ignore probe, rank by DB only |

## Scoring rules (the important ones)

**Passive mode (default)**: ranks purely by recent-window DB fail% — lower fail% = higher tier; only after `dead_streak` (default 2) consecutive `≈100%` rounds does it sink to `dead` (priority `1`, but **not disabled**). More failures → lower tier, successes → higher tier, fully automatic.

**Probe mode (optional)** layers these extra constraints (learned from real data):

- **A single probe failure is only a weak signal** — never kill a credential the DB shows healthy (real example: `codex-muyuan` returns 403/Cloudflare on probe but only 20% fail in the DB over 7 days → trust the DB, keep it).
- **A single probe OK doesn't crown it either**: when the DB shows lots of failures, rank by DB fail% and use OK only as a "not below flaky" floor.
- **Death needs confirmation**: a unified `condemn_streak` counter increments whenever evidence says "dead-worthy" (`PERM_FAIL` probe **or** DB ≈100%); only after `dead_streak` (default 2) consecutive condemned rounds is it actually set `dead` (openai-compat also `disabled`). Any `OK` / `TEMP_FAIL` / DB-healthy round resets it, so **recovery happens automatically**.
- **Temporary failures self-heal**: `TEMP_FAIL` always parks at `resting` (default 150, only above dead); a single OK probe next round lifts it straight back to its health tier.

Tiers (tunable in `.env`):

| Tier | Priority | Trigger |
|------|----------|---------|
| healthy | 600 | fail% < 15% |
| good | 500 | < 30% |
| usable | 400 | < 50% |
| flaky | 300 | 50–75% |
| poor | 200 | hard-failing but still under condemn confirmation / historically usable |
| resting | 150 | temporarily out of budget (quota/rate-limit), awaiting recovery |
| dead | 1 | repeatedly-confirmed ~100% failure; kept as fallback, openai-compat also disabled |

Entries with `priority >= CPA_PIN_FLOOR` (default 1000000, i.e. nothing pinned by default) are treated as **locked** (premium OAuth / manual overrides); the keeper never rewrites them.

## Cross-round state & anti-flap

Each round's result is written to `state.json` (atomic write, self-healing if corrupt):

- `ok_streak` / `condemn_streak` / `temp_streak`: streak counters, computed solely by the scorer (state is a dumb store — no duplicated counter logic).
- `last_priority` / `last_tier` / `dead_since` / `first_seen`: for anti-flap, recovery confirmation, and reporting.
- Credentials no longer present in the config are `prune`d.

The scorer **owns** all streak transitions and returns them in a `Decision`, guaranteeing "the counter that gates death" can never drift out of sync with the scoring rules.

## Safety design

- **Passive by default**: ranks purely from usage history, sends zero extra requests, and **never disables** — a dead relay only sinks to priority `1` (still eligible for fallback), so the DB can observe it recovering. Disabling (`CPA_DISABLE_DEAD=true`) is opt-in.
- **Does not touch OAuth auth-files** (left to CPACodexKeeper), avoiding dual-writer conflicts.
- Writes back a **verbatim-copied full list**, mutating only `priority`/`disabled`, losing no field; strips server-injected read-only fields (`auth-index`).
- PUTs only when a value actually changed.
- Death needs confirmation — `dead_streak` consecutive `≈100%`-fail rounds (and, in probe mode, a probe failure never kills on its own; the DB must also confirm).
- Never auto-**re-enables** an entry you manually disabled — unless there is positive evidence (DB clearly succeeding, or a probe OK).
- Never touches anything at/above the pin floor.
- `--dry-run` lets you preview every change before applying.

## Configuration

Copy the template and fill it in:

```bash
cp .env.example .env
```

Only 2 are required (passive mode):

- `CPA_ENDPOINT`: CPA management API address (e.g. `http://127.0.0.1:8317`)
- `CPA_TOKEN`: CPA management key
- `CPA_CLIENT_API_KEY`: any client key under `api-keys:` in `config.yaml` — **only needed if you enable probing** (`CPA_ENABLE_LIVE_PROBE=true`)

The rest have sensible defaults — see the comments in `.env.example`. Leaving `CPA_USAGE_DB` empty auto-locates the sibling `cpa-usage-keeper_*/data/app.db`.

## Running

Standard library only, Python 3.11+, no third-party dependencies.

```bash
# Dry run (no writes, prints the changes it would make) -- recommended first
python main.py --once --dry-run

# Run one round
python main.py --once

# Daemon mode (default, re-scores every CPA_INTERVAL)
python main.py
```

> **For the first cleanup, run `--once` twice**: because death requires `dead_streak` (default 2) consecutive confirmations, the first round demotes dead relays to `poor` and only the second actually sinks them to `dead` (priority `1`). This is intentional anti-flap.

## Auto-start

Wired into the repo-root `start-cpa-all.ps1`: starting the whole CPA stack also launches this keeper (hidden window, logs to `logs/`, prefers `.venv`, falls back to `python.exe` on PATH).

## Project layout

```text
CPAPriorityKeeper/
├─ src/
│  ├─ settings.py       # .env load + validate + tiers / thresholds / anti-flap params
│  ├─ cpa_client.py     # CPA management API (GET + full-list PUT)
│  ├─ usage_db.py       # read-only SQLite, recent-window health by auth_index/lookup_key
│  ├─ prober.py         # real-request probe + 4-state classification (OK/TEMP/PERM/INCONCLUSIVE)
│  ├─ scorer.py         # probe + DB + cross-round state -> Decision (streaks/recovery/anti-flap)
│  ├─ state.py          # state.json dumb store (atomic write, thread-safe)
│  ├─ maintainer.py     # orchestration: fetch / concurrent probe+score / full-list PUT
│  ├─ logging_utils.py  # concurrent buffered logging
│  └─ cli.py
├─ main.py
├─ tests/
├─ .env.example
├─ LICENSE
└─ README.md
```

## License

[MIT](LICENSE)

# CPAPriorityKeeper

[中文](README.md) | [English](README.en.md)

CPAPriorityKeeper 是一个用于**自动调整 CPA 中转 / API-Key 类提供商优先级与启用状态**的后台工具，是 [CPACodexKeeper](https://github.com/Amiyadesi/CPACodexKeeper) 的姊妹项目。

- **CPACodexKeeper** 管理 codex **OAuth 账号**（auth-files）：失效删除、按配额禁用/启用、临期刷新。
- **CPAPriorityKeeper**（本项目）管理 **api-key / 中转类提供商**（配置里那一堆 `priority: 1000`）：根据真实健康度重排优先级，把失效中转沉底，并让恢复的中转自动回升。

两者互补，分别写不同的凭据类型，互不争抢。

> **默认是「被动模式」**：只读 Usage Keeper 的历史成功/失败率来重排优先级，**不发任何额外探测请求，零额度消耗**。失败多 → 优先级降低，成功 → 优先级升高，全自动。失效中转只会沉到优先级 `1`（保留兜底），**默认不禁用**——因为禁用后它再也拿不到流量，DB 就永远看不到它恢复（单向陷阱）。留在 `1` 还能吃到偶发兜底流量，额度恢复时 DB 看到成功就会自动把它抬回去。
>
> 需要更激进的实时判活时，可在 `.env` 打开 `CPA_ENABLE_LIVE_PROBE=true`（会对每个可路由提供商发真实请求，消耗额度），以及 `CPA_DISABLE_DEAD=true`（对确认失效的 openai-compat 条目额外置 `disabled`）。

> 本项目面向**已授权的本地 / 内部维护场景**：你自己的 CPA 实例、你自己的中转账号池。请勿用于未授权的目标。

## 它解决什么问题

CPA 的 `config.yaml` 里通常有几十个 `codex-api-key` / `openai-compatibility` / `gemini-api-key` / `claude-api-key` 提供商，绝大多数都堆在 `priority: 1000`——在路由里完全是平的，谁先谁后基本随机。更糟的是，一些**早已失效**的中转反而被手动设成了 `10000`，在 `fill-first` 策略下被**优先**使用，导致请求大量失败。

而中转的可用性是**动态**的：

- 这一轮额度用完了 → 一直失败；下一轮额度恢复 → 又能用了。
- token 被吊销 / 账号被封 → 永久失效。
- 上游 Cloudflare / 路由抖动 → 偶发失败，但凭据本身没问题。

CPAPriorityKeeper 的目标就是**区分这些情况**，把真正能用的排前面，把临时没额度的「歇着」，把永久失效的沉底或禁用，并且在它们恢复后自动让其回到队列前列。

## 每轮做什么

1. 从 [CPA Usage Keeper](https://github.com/Willxup/cpa-usage-keeper) 的 SQLite 读取**最近 N 天每个凭据的真实成功/失败率**（只读，按 `auth_index` / `lookup_key` 关联到配置条目）。
2. 把「历史健康度」+「跨轮状态」结合，算出目标优先级档位（失败率越低档位越高）。
3. 仅当优先级（或 openai-compatibility 的 `disabled`）确实变化时，通过 CPA 管理 API 以 **整表 PUT** 的方式原子回写。

> **可选探测（默认关闭）**：打开 `CPA_ENABLE_LIVE_PROBE=true` 后，会额外对每个**带 prefix 可路由**的提供商发一个**真实请求**（默认提示词：「写一个解压zip文件的python脚本，只要核心代码」），把实时结果叠加进评分。这会消耗额度，仅在需要主动判活时启用。

## 优先级方向（已对源码确认）

CLIProxyAPI 的 `sdk/cliproxy/auth/selector.go:getAvailableAuths` 选取**最大** priority 档位，缺省/未设 == `0` == 最低。

> **数字越大 = 越优先被使用**；最差的中转给 `1`（保留为最后兜底，不删除）。

## 为什么用 PUT 而不是 PATCH

实测确认：CPA 管理 API 的 `PATCH /v0/management/<type>` 处理器对每种凭据类型使用**字段白名单**结构体，`priority` **不在白名单内**会被**静默丢弃**（只有 openai-compatibility 的 `disabled` 在白名单里，所以会生效）。因此设置 `priority` 的**唯一**可靠方式是 `PUT` 整个列表——PUT 会把请求体反序列化成完整的 entry 结构体（含 `priority`）。

本项目据此采用：`GET` 整表 → 逐条评分 → 在**逐字复制**的列表上只改 `priority`/`disabled` → `PUT` 整表。其它字段、顺序均不受影响。

## 探测结果四态分类（仅在开启探测时生效）

> 被动模式下没有探测，本节可跳过。仅当 `CPA_ENABLE_LIVE_PROBE=true` 时，`prober.classify()` 把一次探测映射为四种语义，是「区分临时失效与永久失效」的关键：

| 桶 | 含义 | 触发 | 处理 |
|----|------|------|------|
| `OK` | 当前真的能用 | 拿到真实回答文本 | 强正信号 |
| `TEMP_FAIL` | 临时没额度，会自己恢复 | 配额/限流/余额：`429`、`402`、`额度不足`、`quota`、`rate limit`、`balance`… | 沉到 `resting` 档，**绝不判死**，下轮恢复后立刻回升 |
| `PERM_FAIL` | 永久失效 | token 失效/吊销/封号/Cloudflare：`401`、`403`、`invalid token`、`revoked`、`forbidden`… | 计入 condemn 连击，达阈值才判死 |
| `INCONCLUSIVE` | 路由抖动/超时，不能归咎凭据 | `unknown provider`、`model_not_found`、`5xx`、超时；或无法归属的池化条目 | 忽略探测，纯按 DB 排 |

## 评分逻辑（关键规则）

**被动模式（默认）**：纯按 DB 近窗失败率定档——失败率越低档位越高，`≈100%` 连续 `dead_streak`（默认 2）轮才沉到 `dead`（优先级 `1`，但**不禁用**）。失败多 → 降档，成功 → 升档，全自动。

**探测模式（可选）**叠加以下约束（来自真实数据）：

- **单次探测失败只是弱信号**，绝不能据此干掉一个 DB 显示健康的凭据（实例：`codex-muyuan` 探测返回 403/Cloudflare，但 DB 近 7 天失败率仅 20% → 信任 DB，保留）。
- **单次探测成功也不能立刻封神**：DB 显示大量失败时，按 DB 失败率定档，OK 只作为「不低于 flaky」的地板。
- **判死需要确认**：一个统一的 `condemn_streak` 计数器，每当证据指向「该死」（`PERM_FAIL` 探测 **或** DB ≈100%）就 +1；只有连续 `dead_streak`（默认 2）轮被判才真正置 `dead`（openai-compat 同时 `disabled`）。任何 `OK` / `TEMP_FAIL` / DB 健康轮都会清零，**恢复自动发生**。
- **临时失效会自愈**：`TEMP_FAIL` 永远停在 `resting` 档（默认 150，仅高于 dead），下一轮探测 OK 立刻回到健康档位。

档位（可在 `.env` 调整）：

| 档位 | 优先级 | 触发条件 |
|------|--------|----------|
| healthy | 600 | 失败率 < 15% |
| good | 500 | < 30% |
| usable | 400 | < 50% |
| flaky | 300 | 50–75% |
| poor | 200 | 硬失败但仍在 condemn 确认中 / 历史可用 |
| resting | 150 | 临时没额度（配额/限流），等待恢复 |
| dead | 1 | 连续确认 ~100% 失败；保留兜底，openai-compat 同时禁用 |

`priority >= CPA_PIN_FLOOR`（默认 1000000，即默认不锁定任何条目）的条目视为**锁定**（premium OAuth / 手动覆盖），keeper 永不改写。

## 跨轮状态与防抖

每轮结果写入 `state.json`（原子写，损坏可自愈）：

- `ok_streak` / `condemn_streak` / `temp_streak`：连击计数，由 scorer 统一计算（state 只是哑存储，绝不重复计数逻辑）。
- `last_priority` / `last_tier` / `dead_since` / `first_seen`：用于防抖、恢复确认和报告。
- 配置中已不存在的凭据会被 `prune` 清掉。

scorer **独占**全部连击状态转移并通过 `Decision` 返回，确保「判死所用的计数」与「评分规则」永不脱钩。

## 安全设计

- **被动模式默认不禁用任何条目**：失效中转只沉到优先级 `1` 而非 `disabled`——禁用会切断流量，DB 就再也看不到它恢复（单向陷阱）。需要禁用须显式开启 `CPA_DISABLE_DEAD=true`。
- **不接管 OAuth auth-files**（交给 CPACodexKeeper），避免双写打架。
- 回写时发送**逐字复制的完整列表**，仅改 `priority`/`disabled`，不丢任何字段；剥离服务端注入的只读字段（`auth-index`）。
- 仅当值确实变化才 PUT。
- （探测模式）探测失败永不单独判死，必须 DB 也确认，且需连续 `dead_streak` 轮。
- 永不自动**重新启用**用户手动禁用的条目——除非有正向证据（DB 明确成功，或探测 OK）。
- 锁定档位（pin floor）以上从不触碰。
- 默认 `--dry-run` 可先预览全部改动再应用。

## 配置

复制模板并填写：

```bash
cp .env.example .env
```

仅 2 项必填（被动模式）：

- `CPA_ENDPOINT`：CPA 管理 API 地址（如 `http://127.0.0.1:8317`）
- `CPA_TOKEN`：CPA management key
- `CPA_CLIENT_API_KEY`：**仅探测模式必填**——`config.yaml` 里 `api-keys:` 下的任一客户端 key（被动模式不发请求，可留空）

其余项见 `.env.example` 注释，均有合理默认值。`CPA_USAGE_DB` 留空会自动定位同级 `cpa-usage-keeper_*/data/app.db`。

## 运行

仅标准库，需 Python 3.11+，无第三方依赖。

```bash
# 演练（不写回，打印将要做的改动）—— 建议首次先这样跑
python main.py --once --dry-run

# 执行一轮
python main.py --once

# 守护模式（默认，按 CPA_INTERVAL 周期重评）
python main.py
```

> **首次清理建议连跑两轮 `--once`**：因为判死需要 `dead_streak`（默认 2）轮连续确认，第一轮把失效中转降到 `poor`，第二轮才会真正沉到 `dead`（优先级 `1`）。这是有意的防抖设计。

## 自动启动

已接入仓库根目录的 `start-cpa-all.ps1`：启动整套 CPA 栈时会一并拉起本 keeper（隐藏窗口，日志写入 `logs/`，优先用 `.venv`，否则回退到 PATH 上的 `python.exe`）。

## 项目结构

```text
CPAPriorityKeeper/
├─ src/
│  ├─ settings.py       # .env 加载 + 校验 + 优先级档位 / 阈值 / 防抖参数
│  ├─ cpa_client.py     # CPA 管理 API（GET + 整表 PUT）
│  ├─ usage_db.py       # 只读 SQLite，按 auth_index/lookup_key 取近窗健康度
│  ├─ prober.py         # 真实请求探测 + 四态分类（OK/TEMP/PERM/INCONCLUSIVE）
│  ├─ scorer.py         # 探测 + DB + 跨轮状态 -> Decision（含连击/恢复/防抖）
│  ├─ state.py          # state.json 哑存储（原子写，线程安全）
│  ├─ maintainer.py     # 编排：拉取 / 并发探测评分 / 整表 PUT 回写
│  ├─ logging_utils.py  # 并发缓冲日志
│  └─ cli.py
├─ main.py
├─ .env.example
├─ LICENSE
└─ README.md
```

## License

[MIT](LICENSE)

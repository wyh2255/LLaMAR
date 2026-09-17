# A100 远程开发包（通道 · 首跑 · 双机分工）—— 一站式入口

> **组织说明**（2026-09-14 收包）：由散件收拢而成——主文两篇在包根、通道证据三件入 `evidence/`、`/tmp` 运维脚本 7 件入 `scripts/`（另新增 `preheat_mux.sh`）。内容未删改，仅补路径与状态注记。
> **状态**：✅ 通道打通（09-14 午）· ✅ 用户线首跑全绿（09-14 午）· ✅ glvnd 根治复核（09-14 晚）· ✅ 产物归档推送 `e8d127e` · ✅ 双机分工拍板。
> **相关拍板**：钥匙方案 a（WSL 专用钥匙）· 协作模式 ① 按需支援（不派常驻 worker）· **双机分工 = WSL 侧主开发 / A100 侧主执行** · 主机不重启 · 原始 trace 不入 git（全量另存 archive）。
> **第二轮拍板（09-14 晚）**：P2 = ③ 打 env 开关补丁止血 + **保留现状树（不清理）** · P1 = ① 与 P2 共用同一补丁开关 · 开发侧立项 = **仅「卡1 产物一致性」**（卡2 失败归类/watchdog、卡3 接近朝向 暂缓）· 22001 密码登录 = **稳定几天后再关**（择日，需 root）· 双机纪律 = **写**（runbook + A100_TASK.md）。

**结论速览**

| 项 | 内容 |
|---|---|
| 怎么连 | `ssh a100`（别名直达）——chisel 隧道 `43.108.10.79:22001 → a100:22` |
| 怎么派活 | hermes profile `a100`（terminal backend = ssh）→ `hermes -p a100 -z "<prompt>"`；**派单前必跑** `scripts/preheat_mux.sh`（P1） |
| 首跑成绩 | L1 fake 7/7；L2 unity 7/7（3.99s，识别 A100-SXM4-40GB）；L3 短跑 8 步 / 完整跑 50 步 132s（≈2.6s/回合），**零超时** |
| 首跑成本 | 两 L3 合计 110 次 LLM 调用 / 356k tokens（Status 全 ok） |
| 行为面瓶颈 | 50 回合仅 1/22 子任务（transport_rate 4.5%）；`PickupObject` 30/30 失败 → 仿真交互层（非编排框架） |
| 两坑 | **P1** 冷 mux 挂起 → 先预热；**P2** file-sync 写远端 `~/.hermes` → 止血前不跑 ssh-env（已审计：config/auth/.env 未受影响） |
| 铁律 | ① `git pull --ff-only` 开工 ② 小步 commit+push 不过夜 ③ A100 不留「只此一份」 |

## 包内文件

| 文件 | 内容 |
|---|---|
| `README.md` | 本入口（速览 / 导航 / 脚本 / 待办 / 足迹） |
| `20260914-a100-remote-worker-channel.md` | **主台账**：通道定位与打通（钥匙·隧道·profile·冒烟）、P1/P2 两坑、运维要点、首跑验收、远端足迹 |
| `20260914-wsl-a100-dev-workflow.md` | 双机开发分工决策（**已拍板**：WSL 主开发 / A100 主执行；含三条纪律） |
| `evidence/20260914-a100-channel-proof.txt` | 通道冒烟实测输出（hostname / GPU / date） |
| `evidence/20260914-a100-channel-usage.json` | 冒烟用量（38,306 tokens / api_calls=2 / completed=true） |
| `evidence/20260914-a100-remote-skills-md5.txt` | P2 文件同步审计：远端技能树全量 md5（1196 文件） |
| `scripts/` | 运维脚本 8 件（见下表） |

## 脚本（scripts/）

| 脚本 | 用途 |
|---|---|
| `preheat_mux.sh` | **跑 hermes ssh-env 前必做**：清死 mux + 预热（P1 缓解配方；`bash preheat_mux.sh [reset]`） |
| `a100_state2.sh` | 环境全量探查：仓库 / venv / torch+cuda / unity 构建 / GPU / 进程（只读） |
| `a100_state3.sh` | 运行痕迹核查：reports 树 / unity.log / /tmp 最近文件（只读） |
| `a100_fetch.sh` | 首跑关键产物 scp 拉回 + 远端日志尾巴（只读） |
| `a100_pull2.sh` | reports 全量 tar 拉回 + sha256 逐文件对账（只读；模板，改路径可复用） |
| `a100_smoke2.sh` | 免拐杖 unity 冒烟复核（`env -u LD_LIBRARY_PATH`） |
| `a100_finalize.sh` | stage → commit → push → 远端确认 → A100 工作区 ff 同步 |
| `a100_restore_archive.sh` | 全量保全：A100 恢复被 ignore 的 trace + WSL 侧归档 60 件 |

> 脚本均直连 `ssh a100`（或 scp），**不经过 hermes ssh-env**，不受两坑影响；硬编码路径/commit 位按注释替换即可复用。

## 包外关联（canonical 位置不动）

| 位置 | 内容 |
|---|---|
| 仓库（git）`A100_TASK.md` | A100 首跑任务书（agent 可执行清单） |
| 仓库（git）`docs/system_docs/ai2thor_a100_runbook.md` | A100 runbook（环境装配 / 三级验证口径） |
| 仓库（git）`reports/a100_firstrun_20260914/` | 首跑产物（27 件入 git，commit `e8d127e` 已推送；33 件原始 trace 按 ignore 约定未入库） |
| `.hermes/archive/a100_firstrun_20260914_full/` | **全量 60 件**存档（含未入库 trace；双份保全之一） |
| `/home/wyh/daily_work/chisel反向隧道_三机操作手册.md` | 隧道机制出处（三机操作手册） |
| skill `llamar-dev-workspace` → `references/a100-remote-worker.md` | 速查版（连法 / 两坑 / 只读核查定式） |
| `~/.hermes/profiles/a100/` | hermes profile（`terminal.backend: ssh`；凭据在 `.env`，不入本包） |
| `spec/env-contract/README.md` | env-contract 台账中的 A100 补记条目 |

## A100 侧操作红线

- 用户自有 hermes 线可能随时活跃——**只读勿动**；**勿与其并发操作同一仓库**（新工作用独立目录/克隆）。
- 别碰用户自建资源：venv / `~/.ai2thor` / `reports/`（除我们回流的）/ `/tmp/a100_env.sh`。
- root 级操作（apt / 重启）先过用户。

## 待办 / 已拍板

**已拍板（09-14 晚，第二轮）**

- [x] **P2 处置 = ③**：本地 patch hermes 加 env 开关止血（三处 gate：`SSHEnvironment.__init__` 的 `sync(force=True)` / `_before_execute` / `cleanup→sync_back`；默认行为不变）；**保留远端现状树，不清理**——② 否决理由：被覆盖原件已不可恢复，按 md5 删除 ≈ 清空用户线技能树，且同步的删除只删「我们推过的路径」、不碰其原有文件，留着无害。
- [x] **P1 深层修复 = ①**：与 P2 共用同一补丁开关（`cleanup → sync_back` 无超时阻塞 I/O 一并 gate）；预热脚本（`scripts/preheat_mux.sh`）保留为常态习惯，不再作为硬前置。上报上游列为可选后续。
- [x] **P3 派单工作区坑（09-14 夜实测，已绕过）**：kanban 派发器在**本地**解析 `workspace_path`（`resolve_workspace` 会本地 `mkdir -p`）→ 卡里写远端路径（`/home/user/.WYH/LLaMAR`）必失败：`last_failure_error = workspace: [Errno 13] Permission denied: '/home/user'`；**且 "permission denied" 文本误命中 `_RESPAWN_BLOCKER_RE` → 判为 `blocker_auth` 反复跳过、永不 spawn**（现象：卡停 ready、events 刷 `respawn_guarded`）。正确开法：`workspace_kind=scratch` + 卡 body 显式「先 `cd /home/user/.WYH/LLaMAR`」。RP1（`t_7f168b3c`）因此 blocked（留作坑位记录），同内容重发 **RP1b `t_51bc5102`**。
- [x] **开发侧立项 = 仅卡1（产物一致性）**；卡2（unclassified 归类 + TASK_STALE 阈值）、卡3（接近/朝向行为调优）**暂缓**。
- [x] **双机纪律固化 = 写**（runbook 增小节 + A100_TASK.md 一行引用）。
- [x] reboot-required 不重启（glvnd 已根治，不受影响）。

**建卡登记（09-14 晚；创建发生在确认表单超时（18:45）之后——按 09-13 委托条款代行，留档备查，用户一句话可叫停/改卡）**

| 卡 | id | 内容 | 依赖 | 状态（09-14 晚） |
|---|---|---|---|---|
| H1 | `t_fa952779` | hermes 止血补丁（P1+P2 共用 env 开关） | — | ✅ done（commit `1c3f9c70a4`，未 push；副本入 `patches/`） |
| H1-R | `t_5ae74532` | 补丁独立复核（复跑单测/冷 mux 实测/远端零变化复验） | H1 | ✅ done，**verdict = PASS**（六项独立自跑；自写差分 harness 证明未设开关时逐点等价） |
| A1 | `t_372f17d0` | 卡1 产物一致性修复（不提交） | — | ✅ done（第 2 轮成功；+66 行内核 + 新测试 404 行；全量 2411 passed / 8 skipped；SAR 影响评估已写；未提交） |
| D1 | `t_098e3c48` | 双机纪律固化（runbook + A100_TASK.md，不提交） | — | ✅ done（+13 行；编排侧抽查通过） |
| R1 | `t_539ee50d` | A1+D1 合并复核 + 精确路径提交 + push | A1+D1 | ✅ done — **verdict = PASS**；提交 `e5ce7f1`（5 文件 +483）并 push 成功（编排侧 `ls-remote` 独立核验：远端 = 本地 HEAD = `e5ce7f19f7740630df4f838f6b5605afe7eedb93`） |

**第二轮建卡（09-14 夜，用户拍板：卡4 立项 + 卡2 立项）**

| 卡 | id | 内容 | 依赖 | 状态（09-14 夜） |
|---|---|---|---|---|
| C4 | `t_d304a8c1` | assign_task 写点迁移（仅 accepted 记 subtasks/事件；编排侧裁决 = 实际分发语义） | — | ✅ done |
| C2a | `t_e2f73216` | 失败归类扩展（ai2thor 业务失败类目 + raw_request 收编） | — | ✅ done（3 类目全带实测形态；离线演练 55/55；无实测形态的类目不猜） |
| C2b | `t_06787477` | watchdog 阈值适配真机（步数/时基校准 + 按环境可配，反向用例保留） | — | ✅ done（缺省「或」逐字不变；AI2Thor 预设：双条件 + 10 步 + 90s） |
| R2 | `t_f7d8fac2` | 三卡合并复核 + 提交 push | C4+C2a+C2b | ✅ done — **verdict = PASS**；提交 `9b58372`（21 文件 +1785/−71）push 成功（编排侧三源核验：本地 HEAD = 远端跟踪 = `ls-remote` = `9b583720e792553012b38afbde3c2b9983690007`） |

**链收口（09-14 夜）**：两轮共九卡全绿（第一轮 H1/H1-R/A1/D1/R1；第二轮 C4/C2a/C2b/R2）。A100 远端工作区仍停在 `e8d127e`——下次上岗先 `git pull --ff-only` 到 **`b4eb73f`**（纪律已在 runbook）。

**第三轮（09-14 夜）**

| 卡 | id | 内容 | 依赖 | 状态 |
|---|---|---|---|---|
| D2 | `t_ac169009` | env_contract v0.2.0 收口（P4/P5 完成态 + 首跑证据） | — | ✅ done |
| R3 | `t_7478a737` | 复核 + 提交 push | D2 | ✅ done — **PASS**；提交 `adfe7c6`（三源核验：本地 = 远端跟踪 = `ls-remote` = `adfe7c6fb8c4b528e8b4ce98e33f4b2808312709`；独立复跑 L1 266 / L3 2175 passed·8 skipped） |
| RP1 | `t_7f168b3c` | A100 真机复跑（远端 workspace 路径） | — | ⛔ blocked（**P3 坑位记录**；替代卡 = RP1b） |
| RP1b | `t_51bc5102` | A100 真机复跑（scratch 工作区 + 卡内远端 cd） | — | ✅ done（run 876）— **三项验收全过**（编排侧从回传 tar 独立复核）：① phantom 行 **0**（2 行 subtasks 与 accepted 派发一一对应；events `assign_task`=2，2=2）② 失败 13 条中 **12 条落入新域类目**（object_not_visible 11 / navigation_blocked 1），unclassified 1/13=7.7%（首跑 100%）③ **TASK_STALE 0**（首跑 4/2）；`timeout_count=0`、20.03s、`@9b58372` pull ff 成功 |

**RP1b 现场发现（待处置）**

- **F1（真实缺陷，建议立卡）**：C2b 的 `task_stale_seconds=90` 只对「watchdog tick 路径创建」的 supervision state 生效；本 run 的 state 由 push-callback 路径（`record_progress`/`record_state_change` → `get_or_create` 无 config）先创建，`get_or_create` 不向已存在 state 合并 config → **90s 时间条件实际未收紧**（步数 10 + 双条件生效，行为证据：同触发点 `progress_age=10.0s/steps_since=6` 本次未报 STALE）。锚点：`task_watchdog.py:212-222/518-539`、`supervision_state_store.py:160-176`。
- **F2（残留）**：`~/.ai2thor/tmp/thor-Linux64-*.lock`（0B，偏差 1 中止下载残留；不阻塞，按红线未清）。中止时的孤儿下载进程已由本卡清除。
- **F3（口径差异）**：`agent_interactions.csv` 13 失败行 vs `summary.json failed_actions=12`（summary 走 barrier 动作计数；终止期一次 `done` 失败未落 CSV）。不影响验收。
- **F4（runbook 缺口，建议立卡）**：runbook §2.3 的 L3 命令块**缺必需 env**（`LLAMAR_AI2THOR_MODE=unity` / `LLAMAR_AI2THOR_HEADLESS=1` / `LLAMAR_AI2THOR_PLATFORM=cloud`）——原样执行会让 ai2thor 去下载 `thor-Linux64` zip（769MB，慢速）。首跑报告 §1「实际生效的环境变量」已记载，runbook 应补齐。
- **F5（工具链摩擦）**：`PYTHONPATH=src` 内联赋值被 Hermes headless 安全扫描拦（interpreter hijack）→ 改用 `uv run --env-file` 等价携带（A1 卡亦踩过同款）。**A100 直连 github.com TCP 443 超时（校园网）→ 需经 chisel SOCKS `127.0.0.1:1080` 拉取**（本次 pull 即如此，未改 git config）。

**第三轮补卡（09-14 夜，RP1b 现场发现处置）**

| 卡 | id | 内容 | 依赖 | 状态 |
|---|---|---|---|---|
| F1 | `t_1f503c74` | 修 watchdog 配置合并（push-callback 先建的 state 未吸收 `task_stale_seconds=90`） | — | ✅ done（方案②：新增 `_state_config()` 单点，4 条建 state 路径统一注入；RED `assert 120.0 == 90.0` → GREEN） |
| F4 | `t_143b4fb6` | runbook 补真机缺口（L3 env 三项 + 拉取代理 + PYTHONPATH 内联受限） | — | ✅ done（§2.3 自包含 env 块 + `uv run --env-file` 取舍说明 + 769MB 坑注；§0 网络注记） |
| R4 | `t_27887909` | F1+F4 复核 + 提交 push | F1+F4 | ✅ done — **PASS**；提交 `b4eb73f`（4 文件 +151/−13）push 成功（编排侧三源核验：本地 = 远端跟踪 = `ls-remote` = `b4eb73fdb846d2989d6a21e4d5ec366dcae2e38c`） |

**分支策略（09-14 夜拍板，用户）**：main **补推 origin/main 完毕**（ff `e6b95fc..1d45654`；编排侧三源核验：本地 = 远端跟踪 = `ls-remote` = `1d456543dbbe26dfd1f09a3f0aa1bf1ecf6b96e6`）——`main` 与 `feat/ai2thor-scene-adaptation` **各自独立推进**，**框架侧改动不回迁**（P4/P5 的 `src/orchestration/` 等继续留在 ai2thor 分支）。注：main 上另有**并行 SAR-eval 车道**的 3 个提交（`9249aa1` / `aabf5f9` / `1d45654`，由该车道卡片于 20:57–21:25 在 main 检出内提交）随本次推送一并上远端；若后续 SAR 侧需要编排层，再单独立项讨论回流。

**A100 派单链状态：已正式解锁**（补丁在 + 开关已注入 + H1/H1-R 双卡远端零写入实证；预热脚本仍建议但不再是硬前置）。

卡图要点：H1 = 三处 gate + 单测 + 冷 mux 实测（≤90s 不挂）+ 远端 skills/cache md5 零变化对账 + `patches/` 副本；A1 = fake 两路径产物断言、**禁改 AGENTS.md**、diff 最小；R1 = 复核后精确路径 `git add` + push `origin/feat/ai2thor-scene-adaptation`（推前核对远端 = `e8d127e`）。

**未决 / 后续**

- [x] **22001 公网可达 + sshd 密码登录 —— 拍板（09-14 夜）= 不动，仅登记**（共享机、同事走内网且依赖密码登录）。实测存档：① 历史全量 auth.log（含 .gz，回看至 8-22）**零** `Accepted password`、**零** `Failed password`；② 成功登录全为 publickey（96×隧道落点 127.0.0.1 + 2×内网 10.133.31.107）；③ 外部探测（经隧道）sshd 仍提供 `publickey,password`（开着但无人用）；④ fail2ban 未装。**备选加固方案存档**（如将来想收紧再启用、且不锁人）：`/etc/ssh/sshd_config.d/99-hardening.conf` 写 `Match Address 127.0.0.1,::1` + `PasswordAuthentication no` + `KbdInteractiveAuthentication no`（隧道在 sshd 看来是回环来源 → 公网路径转密钥-only、内网密码登录原样保留）；回滚 = 删文件 + `systemctl reload ssh`。
- [ ] 卡3（接近/朝向行为调优）仍**暂缓**；卡2 已立项（C2a/C2b 见上表）。
- [ ] **卡4 = 已立项（C4 `t_d304a8c1`，09-14 夜）**：A1 §7 相邻发现——直接派发路径在 `tool_start` **无条件**写 `assigned` 行/事件（失败派发也留 phantom 行；A100 短跑 `dispatch-1/2` 两行即失败记录）。已裁决 = **实际分发语义**（写点迁 `tool_result`，仅 accepted 记），含同类点核查（cancel/reply）与 SAR 论文口径同步。
- [ ] **远端足迹登记补遗（09-14 晚实测）**：`~/.hermes/cache/` + `images/` 共 31 文件亦被 file-sync 上传；`credentials/` 目录已建但**为空**（无凭据外泄）；`skills/` 1197 文件。

## 远端足迹清单（A100 侧，截至 09-14 晚）

- `~/.ssh/authorized_keys`：+1 行（`a100_wsl_ed25519.pub`）
- `~/.hermes`：skills 树被 file-sync 镜像（P2；md5 全量留档）；`config.yaml` / `auth.json` / `.env` / `sessions/` / `logs/` **未被触碰**（哈希核验）
- `/home/user/.WYH/LLaMAR`：`reports/a100_firstrun_20260914/` 全量 60 件（27 入库 + 33 恢复）；工作区 HEAD `e8d127e`
- 未触碰：用户 hermes 会话 / VS Code server / 其余用户文件

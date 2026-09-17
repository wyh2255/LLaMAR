# 2026-09-14 A100 远程 worker 通道（跨远程委派能力打通）

> 状态：通道已打通并实测；两处已知坑（P1 冷启动挂起 / P2 file-sync 写远端）均有缓解。**处置已拍板（09-14 晚）：P1/P2 共用同一本地补丁开关止血（P2 保留现状树，不清理）；预热脚本保留为常态习惯。**
> 更新（9-14 晚）：**用户线首跑已完成——L1/L2/L3 全绿**；**产物已回流提交 `e8d127e`**；glvnd 已根治（免 workaround 复核通过）。详见文末「A100 现状」与「后续动作」。
> 用户拍板：**钥匙方案 a**（WSL 专用钥匙）+ **模式 ① 按需支援**（不派常驻 worker）。
> 包：本文件属 `.hermes/spec/a100-remote-dev/` 包（入口 `README.md`；证据 `evidence/`；脚本 `scripts/`）。

## 背景

- 用户提出「能否跨远程委派 A100 上的 worker」，入口线索：「经过跳板机器，正是你现在的代理IP的端口22001」。
- 通道定位（实测）：本机 WSL 代理出口 IP = `43.108.10.79`（Windows 侧代理 7892）；`43.108.10.79:22001` = **chisel 反向隧道** → 远程服务器 22 端口（sshd）。
- 同一性交叉验证：`[43.108.10.79]:22001` 与 Windows `known_hosts` 记录的 A100 内网地址 `172.17.166.37` 的 ECDSA host key 完全相同 → 同一台机。机制出处：《chisel反向隧道_三机操作手册.md》（`/home/wyh/daily_work/`）。
- 登入（实测）：`ssh -i <Windows id_rsa> -p 22001 user@43.108.10.79` → hostname `a100`。
- 机制结论：**可跨远程委派**——调度面（kanban/dispatcher）留本机，远程化发生在 **worker 的终端层**（Hermes 内置 SSH terminal backend：`terminal.backend: ssh` + `ssh_host/ssh_user/ssh_port/ssh_key`，per-profile；terminal/execute_code/文件工具全部落远端）。

## 已落地并实测验证

1. **专用钥匙**：`~/.ssh/a100_wsl_ed25519`（ed25519；fp `SHA256:fT1MCbzYRDECoo3+aNiqlF68I4kYHvuYkEXj3qqrUp4`）；公钥已追加至 A100 `~user/.ssh/authorized_keys`（现共 2 把）；Windows 私钥从未离开 Windows（仅探测期临时拷贝已清理）。
2. **ssh 别名**：`~/.ssh/config` 新增 `Host a100`（HostName 43.108.10.79 / Port 22001 / User user / IdentityFile ~/.ssh/a100_wsl_ed25519 / IdentitiesOnly yes）→ **`ssh a100` 直接可用**。
3. **hermes profile `a100`**：clone 自 coder；terminal 段 = `backend: ssh` / `ssh_host: 43.108.10.79` / `ssh_user: user` / `ssh_port: 22001` / `ssh_key: ~/.ssh/a100_wsl_ed25519` / `cwd: ~`；wrapper `~/.local/bin/a100`。
4. **端到端冒烟**（`hermes -p a100 -z`，agent 实际调用 terminal 工具走 ssh backend）：远端返回 `hostname=a100` / `whoami=user` / `nvidia-smi -L = A100-SXM4-40GB` / `date`，退出码 0；用量 38,306 tokens（api_calls=2，completed=true）。证据副本：`evidence/20260914-a100-channel-proof.txt`（本包）、`/tmp/a100_channel_proof.txt`。

## 已知问题

### P1 冷启动挂起（根因已定位，有缓解配方）

- **现象**：mux 冷状态（无 `/tmp/hermes-ssh/*.sock`）时，hermes ssh-env 运行挂起（>240s，首个 API 调用永不发出）；mux 热时 ~20s 正常。
- **根因**（ABRT 活体堆栈实锤）：`SSHEnvironment.__del__ → cleanup() → sync_back() → _ssh_bulk_download` 在 **config 加载的 GC 阶段**发起**无超时阻塞 I/O**（远端文件同步），主线程 `load_config_impl` 被一并拖住。属 hermes ssh backend 上游行为，非本机环境问题。
- **缓解配方（实测有效）**：运行/派单前**预热 mux**：
  ```bash
  SOCK=/tmp/hermes-ssh/$(python3 -c "import hashlib;print(hashlib.sha256('user@43.108.10.79:22001'.encode()).hexdigest()[:16])").sock
  ssh -o ControlMaster=auto -o ControlPath=$SOCK -o ControlPersist=300 -o BatchMode=yes \
      -i ~/.ssh/a100_wsl_ed25519 -p 22001 user@43.108.10.79 true
  ```
  - socket 名公式 = `sha256("user@host:port")[:16]`（本机实测值 `ac5c4c8da4cfab87`）；`ControlPersist=300` → 预热后 5 分钟内有效。
  - 已挂起时的救济：`pkill -f hermes-ssh; rm -f /tmp/hermes-ssh/*.sock` → 预热 → 重试。
- **附带观察**：即使成功完成工作，进程收尾可能滞留（`rc=124` 为 wrapper 超时强杀，属 cleanup 同步阻塞同族）；dispatch 场景启用前需实测 + 加超时护栏。
- **深层修复（已拍板）**：本地 patch hermes，给 sync 加 env 开关（默认行为不变）；开关关闭 = 跳过三处调用（`__init__` 的 `sync(force=True)` / `_before_execute` / `cleanup→sync_back`）→ P1、P2 一并止血。上报上游列为可选后续。

### P2 file-sync 会写入远端 `~/.hermes`（有实际副作用）

- **机制**：hermes ssh backend 面向**专用容器**设计，会把本机 credentials/skills/cache 同步到远端 `~{user}/.hermes/`（`iter_sync_files` + `FileSyncManager.sync(force=True)`；无关闭开关，`HERMES_FORCE_FILE_SYNC` 只用于强制、不能关闭）。
- **实际发生**（今天 11:50–12:14 数次 ssh-env 运行）：远端技能树 1196 个文件中 **1164 个与本机各 profile 技能库哈希吻合**（上传保留原 mtime、目录 mtime=上传时刻）；32 个为远端原有文件。留档：`evidence/20260914-a100-remote-skills-md5.txt`（全量 md5 清单）。
- **未受影响（哈希核验）**：`config.yaml` / `auth.json` / `.env` / `sessions/` / `logs/` —— 与本机任一副本均不匹配、mtime 早于今天（9-13 及更早），确认未被触碰。
- **影响评估**：远端 skills 被覆盖为"本机镜像"；若其上有同名异容文件则被替换（无法完整重建原状）。功能上 hermes 不受影响（技能为文档类，读入即用）；风险点 = 若 A100 侧自行修改过同名技能，下次同步会再次覆盖。
- **处置（已拍板 09-14 晚）**：**③ 打 env 开关补丁止血 + 保留现状树（不清理）**。② 否决（被覆盖原件不可恢复；按 md5 删除 ≈ 清空用户线技能树）。源码核实（09-14 晚）：`tools/environments/file_sync.py` 的 `iter_sync_files` 固定打包 credentials+skills+cache，`SSHEnvironment.__init__` 无条件 `sync(force=True)`，**无官方关闭开关** → 必须本地 patch（本机 hermes 安装为 git 检出 `4f22543509`，可 patch，升级后需重打）。
- **即日约定（09-14 晚更新）**：止血补丁落地前仍**不跑 hermes ssh-env**；补丁落地后恢复可用，首次实跑需做远端零变化对账（skills/cache md5 前后一致）。诊断随时可走 `ssh a100` 直连。

## 运维要点（模式 ①）

- **诊断**：`ssh a100` 直达（读日志/进程/GPU/网络），不受 P1/P2 影响。
- **派 worker（将来）**：开 `assignee=a100` 卡（卡图先过目）→ 运行前预热 mux（包内 `scripts/preheat_mux.sh`）→ 结束后核查远端足迹。
- **A100 现状（9-14 16:4x 更新）**：用户线**首跑已完成且全绿**——仓库已 checkout `feat/ai2thor-scene-adaptation`@`849275a`；`uv sync --extra ai2thor-unity` 成功（Py3.14.3 / torch 2.12.1+cu130 / cuda True / venv 5.2GiB）；Unity CloudRendering build 已缓存（`~/.ai2thor`）；**L1 `exit=0`、L2 unity 冒烟 `exit=0`（7/7 checks，3.99s，真机识别 A100-SXM4-40GB）、L3 短跑(8步/28.04s)与完整跑(50步/132.22s) 均真机真 LLM 完成且 `timeout_count=0`**。报告与产物：`~/.WYH/LLaMAR/reports/a100_firstrun_20260914/`（`A100_FIRSTRUN_REPORT.md` 20KB）；关键产物本机副本 `/tmp/a100_firstrun/`；全量 60 件持久存档 `.hermes/archive/a100_firstrun_20260914_full/`（含 33 个 ignore trace）。其 hermes 会话可能仍活跃——勿与其并发操作同仓库。
- **首跑遗留（待处置）**：① glvnd **已根治**（用户执行 root 修复；09-14 傍晚复核：四库回位、`dpkg -V` 干净、免 `LD_LIBRARY_PATH` 冒烟 `exit=0`/7 checks/3.38s）；**决定不重启**（reboot-required 仍在，非必须；扩展库 GLU/GLEW/GLES 仍缺但无关本链路）。② 开发侧发现：完整跑 `subtasks.csv` 未生成 / 顶层 `events.ndjson` 缺 `assign_task`（协调路径差异→产物判据假阴性，报告 §4-5）；`unclassified_tool_error` 226 次（失败未归类）+ `TASK_STALE` 阈值偏敏感（§4-4）。③ 任务完成度未达标：50 回合 `transport_rate=0.0455`、`PickupObject` 30/30 失败——判定为仿真交互层瓶颈（接近/朝向不足），非编排框架。
- **远端足迹清单**：`authorized_keys` +1 行（我们的公钥）；skills 树（见 P2）；其余未动。

## 后续动作

- [x] 用户拍板（09-14 晚）：P2 = ③（补丁止血 + 保留现状树）；P1 = ①（与 P2 共用同一开关）；上报上游 = 可选后续。
- [x] A100 首跑（用户线，9-14 12:15–12:25；L1/L2/L3 全绿，见上）。回流：**已提交推送 `e8d127e`**（`reports/a100_firstrun_20260914/`：27 件入 git；33 件原始 trace 按 `*.ndjson`/`*.log` ignore 约定未入库，全量 60 件另存 WSL `.hermes/archive/`）；A100 工作区已 ff 同步并恢复全量文件。
- [ ] 开发侧修复卡：① 产物一致性（§4-5）——**已立项（09-14 晚拍板）**，待卡图确认后开跑；② 工具失败归类 + watchdog 阈值（§4-4）——**暂缓**。
- [x] A100 侧 glvnd 根治（用户执行；复核通过）；重启——用户拍板不做（非必须）。
- [ ] 后续远程复跑/修复：按「运维要点」开 a100 卡（含预热步骤）。
- [x] 双机开发分工已拍板（09-14 晚）：WSL 主开发 / A100 主执行——见 `20260914-wsl-a100-dev-workflow.md`；纪律固化 = **写**（runbook + A100_TASK.md，随写作卡落地）。

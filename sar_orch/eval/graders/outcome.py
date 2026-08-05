import ast
import json

from sar_orch.eval.dataset import ENV_ACTION_NAMES, AgentInteraction, EpisodeDataset
from sar_orch.eval.graders.base import GradeResult


def _parse_csv_list(value: str):
    """Parse a Python-literal list out of a CSV cell.

    `ast.literal_eval`, not `eval`: these cells are written by the harness but
    read back as untrusted input -- a trajectory.csv from another machine, a
    shared results dir, or an LLM-authored observation string would otherwise be
    executed. literal_eval accepts exactly the literal syntax actually used here
    and nothing else, at no cost to the valid cases.
    """
    if not value or value == "[]":
        return []
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return []


#: 论文 §5 Metrics 中 Balance 定义里的稳定项，用于避免除零。
_BALANCE_EPSILON = 1e-4

#: 不计入"成功高层动作"的空动作。原始 LLaMAR 参考实现过滤 ["Done", "Idle"]
#: （AI2-THOR 词汇），SAR 里等价的空动作是 NoOp；三者全部保留以兼容两边数据。
_BALANCE_SKIP_ACTIONS = ("NoOp()", "NoOp", "Idle", "Done")


def episode_agent_count(episode: EpisodeDataset) -> int:
    """本 episode 的智能体数 n。

    优先取 metadata（权威值），否则回退到轨迹里 Actions 列的最大长度。
    n 必须覆盖**全部**智能体，否则从未成功过的智能体会被漏掉，
    balance 被高估（例如 2 智能体中一个 0 次成功、另一个 5 次，
    漏算会得到 5/5=1.0，而论文定义应为 0/5≈0.0）。
    """
    meta = episode.metadata or {}
    for key in ("agent_count", "agents", "num_agents"):
        val = meta.get(key)
        if isinstance(val, int) and val > 0:
            return val
    if episode.agent_names:
        return len(episode.agent_names)
    return max(
        (len(sr.actions) for sr in episode.steps.values()),
        default=0,
    )


def compute_balance(episode: EpisodeDataset) -> float:
    """论文 §5 的 Balance 指标。

    B := min{s_1,...,s_n} / (max{s_1,...,s_n} + eps)，其中 s_i 为第 i 个
    智能体成功执行的高层动作数，n 为本 episode 的智能体总数，eps=1e-4。

    B=0 表示至少一个智能体没有任何成功动作；B≈1 表示各智能体贡献相同。
    """
    n = episode_agent_count(episode)
    if n <= 0:
        return 0.0

    # 零填充所有 n 个智能体：从未成功的智能体必须以 0 计入 min。
    agent_success_counts: dict[int, int] = {i: 0 for i in range(n)}
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        for i, (act, suc) in enumerate(zip(sr.actions, sr.successes)):
            if i >= n:
                # 轨迹比 metadata 声明的智能体更多：按实际数据扩展，避免漏算。
                agent_success_counts.setdefault(i, 0)
            if act in _BALANCE_SKIP_ACTIONS:
                continue
            if suc:
                agent_success_counts[i] = agent_success_counts.get(i, 0) + 1

    vals = list(agent_success_counts.values())
    return min(vals) / (max(vals) + _BALANCE_EPSILON)


#: 向后兼容的私有别名（历史调用方使用 _compute_balance）。
_compute_balance = compute_balance


def compute_tool_outcomes(episode: EpisodeDataset) -> dict[str, dict[str, int]]:
    """逐工具的 attempts / succeeded / failed / unknown 计数。

    为什么必须在 eval 层集中产出：DESIGN 3.1b 已论证二元 SR 在任何可负担样本量
    下只能分辨 ≥33 pp 的差异，故**改进证据一律用确定性行为信号** —— 而这些
    信号就是逐动作计数（单个 run 内就有几十到几百次观测，样本量需求低 1-2 个
    数量级）。此前它们只存在于 `compare_cells.py` 里，由那个脚本自己重新解析
    `agent_interactions.csv` 得出，即 E-24 的双份解析：同一个"成功"判定有两处
    实现，环境改一次措辞就会两边不一致而无人察觉。

    成功与否一律取 `ai.succeeded`（T4 集中化的判定），不在此处再匹配字符串。
    `unknown` 单独计数而不并入 failed —— "判不出来"与"判定为失败"是两种状态，
    合并会让解析退化伪装成行为退化。
    """
    out: dict[str, dict[str, int]] = {}
    for step_num in sorted(episode.steps.keys()):
        for ai in episode.steps[step_num].interactions:
            name = ai.tool_name or "unknown"
            rec = out.setdefault(
                name, {"attempts": 0, "succeeded": 0, "failed": 0, "unknown": 0}
            )
            rec["attempts"] += 1
            if ai.succeeded is True:
                rec["succeeded"] += 1
            elif ai.succeeded is False:
                rec["failed"] += 1
            else:
                rec["unknown"] += 1
    return dict(sorted(out.items()))


def _extract_person_id(ai: AgentInteraction) -> str | None:
    """从一次 `drop_off_person` 调用里取出目标 person_id。

    优先解析 `tool_args`（原始 kwargs JSON）——它是权威来源。`action_args`
    已被 `_canonical_action_args` 按声明顺序重排过（`dataset.py:44`），理论上
    `action_args[0]` 也是 person_id，但那次重排本身依赖同一份 `tool_args`
    成功解析；直接读 `tool_args` 避免多一层可能出错的中间表示（T4 的教训：
    同一个值不要有两条派生路径）。`action_args` 仅在 `tool_args` 解析失败时
    兜底，此时它可能就是未重排的原始顺序，宁可拿到一个值去核对，也不要
    直接放弃这条记录。
    """
    if ai.tool_args:
        try:
            parsed = json.loads(ai.tool_args)
        except (json.JSONDecodeError, ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            pid = parsed.get("person_id")
            if pid:
                return str(pid)
    if ai.action_args:
        return str(ai.action_args[0])
    return None


def compute_rescue_outcomes(episode: EpisodeDataset) -> dict[str, dict]:
    """按 person 聚合的 rescue 完成情况 —— 取代"逐次 tool call 成功率"这个
    对"改进"和"重试模式变化"不可辨识的口径。

    为什么原口径（`compute_tool_outcomes()["drop_off_person"]`）不能用来判定
    改进：`drop_off_person` 是 cumulative 协议 —— 一个 person 要等**全部**
    carrier 都调用过才算救出，`skills/worker/person-rescue/SKILL.md` 明确要求
    先到的 carrier 不要 `no_op` 等待，而是持续重试直到最后一个到位。于是
    协议内的合理重试全部被计成"失败的 tool call"：重试轮数越多，原始成功率
    越低，但那恰恰对应"协议被正确遵守"。实证：一批 `transport_rate` 全为
    1.0（人全部救出）的数据，原始 `drop_off_person` 成功率只有 31.0%，
    方向与 transport_rate 相反。

    新口径改成**"这次 rescue 最终是否完成"**：该 person 的所有
    `drop_off_person` 调用里，只要有任意一次 `succeeded is True` 就算完成
    （cumulative 协议下，最后一个到位的 carrier 那次调用才会返回成功）。
    同时把**重试轮数**（该 person 总共发生了多少次 `drop_off_person` 调用）
    单独产出 —— 这正是被原口径混淆掉、这次要让它可见的那个变量。

    三态处理：单次调用的 `succeeded` 可能是 True/False/None。
    - 只要有一次 True，该 person 记 completed=True，`unknown` 调用不影响判定
      （cumulative 协议下真正决定成败的是"是否出现过一次成功"，
      不确定的调用不能推翻已确认的成功）。
    - 若从未出现 True：若存在至少一次 False，判 completed=False（真实失败，
      不是解析问题）；若全部调用都是 None（一次 False 都没有，也没有一次
      True），判 completed=None（"判不出来"，不可当作失败——与
      `compute_tool_outcomes` 的 unknown 单独计数是同一原则）。

    返回结构：
        {
            "<person_id>": {
                "completed": bool | None,
                "attempts": int,        # 该 person 的 drop_off_person 调用总数（= 重试轮数）
                "succeeded_attempts": int,
                "failed_attempts": int,
                "unknown_attempts": int,
                "final_step": int,      # 最后一次调用发生的 step（辅助排查用）
            },
            ...
        }
    没有任何 `drop_off_person` 调用时返回空字典 —— 该 run 应被上游判定为
    "无 rescue 事件"而排除出统计，不能记 0（"没有尝试"与"尝试了但全部失败"
    是两种状态，参照 idle_ratio/compute_tool_outcomes 的既有原则）。
    """
    per_person: dict[str, dict] = {}
    for step_num in sorted(episode.steps.keys()):
        for ai in episode.steps[step_num].interactions:
            if ai.tool_name != "drop_off_person":
                continue
            pid = _extract_person_id(ai)
            if not pid:
                # 连 person_id 都解析不出来：既不能计入任何 person，也不能
                # 悄悄丢弃导致 attempts 总数与 compute_tool_outcomes 对不上
                # ——归入占位桶，让缺口在数据里可见而非消失。
                pid = "unknown_person"
            rec = per_person.setdefault(
                pid,
                {
                    "completed": None,
                    "attempts": 0,
                    "succeeded_attempts": 0,
                    "failed_attempts": 0,
                    "unknown_attempts": 0,
                    "final_step": step_num,
                },
            )
            rec["attempts"] += 1
            rec["final_step"] = step_num
            if ai.succeeded is True:
                rec["succeeded_attempts"] += 1
            elif ai.succeeded is False:
                rec["failed_attempts"] += 1
            else:
                rec["unknown_attempts"] += 1

    for rec in per_person.values():
        if rec["succeeded_attempts"] > 0:
            rec["completed"] = True
        elif rec["failed_attempts"] > 0:
            rec["completed"] = False
        else:
            # 全部调用都是 unknown：判不出来，不是失败。
            rec["completed"] = None

    return dict(sorted(per_person.items()))


def compute_rescue_summary(episode: EpisodeDataset) -> dict | None:
    """把 `compute_rescue_outcomes` 压成 run 级的一个数字，供跨 run 统计用。

    统计单位必须是 run（每个 run 一个 rescue 完成率），不能是 tool call
    ——81 次调用来自 15 个 run，同一 run 内的调用高度相关（同一 episode、
    同一套 agent 策略、同一条重试链），把它们当独立样本会系统性高估显著性。
    这个函数就是"一个 run 压成一个数"的实现，供上层做 run 级检验。

    completed=None 的 person（判不出来）不计入分子也不计入分母 —— 与
    `compute_tool_outcomes` 的 unknown 处理一致，不能把"判不出来"并入
    "失败"从而拉低完成率，也不能并入"成功"从而拉高它。

    没有任何 `drop_off_person` 事件时返回 None（字段缺席），不是 0.0——
    该 run 应被排除出跨 run 统计，而非被记成"尝试了但零成功"。
    """
    per_person = compute_rescue_outcomes(episode)
    if not per_person:
        return None

    completed = sum(1 for r in per_person.values() if r["completed"] is True)
    failed = sum(1 for r in per_person.values() if r["completed"] is False)
    unknown = sum(1 for r in per_person.values() if r["completed"] is None)
    determined = completed + failed
    retry_rounds = [r["attempts"] for r in per_person.values()]

    return {
        "persons_total": len(per_person),
        "persons_completed": completed,
        "persons_failed": failed,
        "persons_unknown": unknown,
        # 分母只用可判定的 person 数；全 unknown 时无法给出比率。
        "completion_rate": (completed / determined) if determined > 0 else None,
        "retry_rounds_per_person": retry_rounds,
        "retry_rounds_mean": (
            sum(retry_rounds) / len(retry_rounds) if retry_rounds else None
        ),
    }


def check_rescue_transport_consistency(episode: EpisodeDataset) -> dict:
    """交叉核对：rescue-level 完成数与 `transport_rate` 应该同向。

    背景：原始 `drop_off_person` 成功率与 `transport_rate` 曾方向相反
    （一批数据里前者 31.0%、后者全为 1.0）——这正是本模块要修的口径缺陷的
    症状。新口径修好之后，二者不应再系统性矛盾；这个函数把这条核对做成
    可复用的检查，而不是要求每次人工翻 CSV 对照。

    "一致"的判定标准：若该 run 的 `transport_rate`（checker 子任务完成率的
    同义字段）达到 1.0（人员运输子任务全部完成），rescue-level 完成率也应
    是 1.0（前提是能判定，即 persons_unknown 未覆盖全部 person）；若二者
    出现"transport_rate=1.0 但 rescue completion_rate<1.0"这种方向冲突，
    标记为 inconsistent 供人工核查（可能是 person_id 解析遗漏，也可能是
    transport_rate 分母里包含非人员子任务导致的巧合，而非真的矛盾——
    这个函数只负责标记，不负责下结论）。

    返回：{"checked": bool, "consistent": bool | None, "reason": str,
            "transport_rate": float | None, "rescue_completion_rate": float | None}
    `checked=False` 表示数据不足以核对（例如没有 rescue 事件，或
    transport_rate 缺失）。
    """
    last = episode.last_step
    transport_rate = last.transport_rate if last is not None else None
    summary = compute_rescue_summary(episode)

    if summary is None:
        return {
            "checked": False,
            "consistent": None,
            "reason": "no drop_off_person events in this run",
            "transport_rate": transport_rate,
            "rescue_completion_rate": None,
        }

    rate = summary["completion_rate"]
    if rate is None or transport_rate is None:
        return {
            "checked": False,
            "consistent": None,
            "reason": "completion_rate or transport_rate unavailable",
            "transport_rate": transport_rate,
            "rescue_completion_rate": rate,
        }

    # 仅在 transport_rate 达到满值时做强判定：未满值时两个指标的分母
    # 不保证同源（transport_rate 分母含非人员子任务），不宜苛求相等。
    if transport_rate >= 1.0 - 1e-9:
        consistent = rate >= 1.0 - 1e-9
        reason = (
            "transport_rate=1.0 and all determinable rescues completed"
            if consistent
            else "transport_rate=1.0 but rescue completion_rate < 1.0"
        )
    else:
        consistent = True
        reason = "transport_rate < 1.0: no strict cross-check applied"

    return {
        "checked": True,
        "consistent": consistent,
        "reason": reason,
        "transport_rate": transport_rate,
        "rescue_completion_rate": rate,
    }


def compute_timeout_steps(episode: EpisodeDataset) -> int:
    """有 agent 超时的步数。429 限流会伪装成性能退化，故必须可见。"""
    return sum(
        1 for sr in episode.steps.values() if getattr(sr, "timeout_agents", None)
    )


def compute_idle_ratio(episode: EpisodeDataset) -> float | None:
    """浪费动作占比 = (NoOp + 失败动作) / 总动作数。

    为什么需要它：`balance` 把"角色分工"当作缺陷惩罚 —— 一个 agent 专职
    灭火、另一个专职搬人时，min/max 天然偏低，但那恰恰是好的协作。
    用它做门禁会把系统推向平均主义。这个**结构性**缺陷是 balance 降级的
    唯一依据。

    ⚠ 曾据"失败 run 0.841 > 成功 run 0.805"称 balance 与成功反相关，
    后补 Mann-Whitney U 检验：U=49.0、p=0.485、效应量 +0.021，**未达显著**
    （同口径对照 idle_ratio 自身：U=50.0、p=0.454、+0.042，同样不显著）。
    即那个相关性声称站不住，勿再引用；降级理由见上一段。

    `idle_ratio` 衡量的是**真正的浪费**：空动作与失败动作。它不惩罚分工 ——
    两个 agent 各干各的、但每一步都有效，idle_ratio 依然是 0。

    与 balance 的关键差别在分母：balance 比较 agent **之间**的产出，
    idle_ratio 只看动作**本身**是否有效，与谁做的无关。

    ⚠ 这是**诊断量**，不是门禁项。新增指标的方向性必须先在既有数据上刻画
    （见 DESIGN 2.2 的 Mann-Whitney 验收），而不是先设阈值再套。

    无任何动作时返回 None（不是 0.0）—— "没有浪费"与"没有数据"是两种状态，
    合并会让空 run 看起来完美。
    """
    total = 0
    wasted = 0
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        # zip 到较短的一方：actions 与 successes 长度不一致时（数据损坏）
        # 宁可少算也不要用 None 冒充失败，那会把解析问题伪装成行为问题。
        for act, suc in zip(sr.actions, sr.successes):
            total += 1
            if act in _BALANCE_SKIP_ACTIONS or not suc:
                wasted += 1
    if total == 0:
        return None
    return wasted / total


#: 环境层动作名白名单 —— 单一来源：dataset.ENV_ACTION_NAMES（constraint /
#: error_taxonomy / outcome 共用同一张表，避免三份重复白名单漂移；别名归一
#: CarryPerson→Carry 等也在 dataset.parse_action 处统一完成）。
_ENV_ACTION_NAMES = ENV_ACTION_NAMES

#: scene 编号 → 覆盖目标名列表 的进程内缓存。场景参数是静态字面量
#: （`SAR/Scenes/scene_*.py` 里写死的 `Arg(...)`），一个 scene 只需解析一次。
_COVERAGE_TARGET_CACHE: dict[int, list[str] | None] = {}


def coverage_targets_for_scene(scene: int | str | None) -> list[str] | None:
    """复算某个 scene 的**覆盖目标名单**，即 `checker.coverage` 的内容。

    为什么能在 eval 层复算：`SAR/Scenes/checker.py:initialize()` 推导 coverage
    的输入**只有场景参数字面量**（`scene_N.py` 里写死的 `Arg(name=...)`），
    不含任何运行期状态、随机种子或 agent 行为。故同一 scene 的 coverage 名单
    是常量，可脱离 env / barrier 静态复算。这与 `checker_subtask_total` 由
    已记录数据反推的先例同类：**测量层重建场景事实，不碰运行时**。

    复算规则逐字对齐 `checker.py:64-76`：
      1. 除 `reservoirs` / `agents` 外所有对象组的 name 全部计入；
      2. reservoirs 只计入与某个 fire 的 `tp` 匹配的那些（"实际会被用到的"）；
      3. `set()` 去重 —— 与 checker 的 `list(set(coverage))` 同。

    无法确定 scene（metadata 缺 scene / 场景文件不存在）时返回 None，
    表示"无从判定"，绝不返回空列表 —— 空列表会让下游把分母当 0 或把
    覆盖率算成满分。
    """
    if scene is None:
        return None
    try:
        scene_num = int(scene)
    except (TypeError, ValueError):
        return None
    if scene_num in _COVERAGE_TARGET_CACHE:
        return _COVERAGE_TARGET_CACHE[scene_num]

    targets: list[str] | None = None
    try:
        import sys
        from pathlib import Path

        # SAR/ 用扁平 import（不是 package），必须把它本身加进 sys.path。
        # 与 `sar_orch/barrier.py:24-27` 同样的处理。
        sar_dir = Path(__file__).resolve().parents[3] / "SAR"
        if str(sar_dir) not in sys.path:
            sys.path.insert(0, str(sar_dir))
        from Scenes.get_scene_init import get_scene_initializer

        scene_mod, _ = get_scene_initializer(scene_num)
        params = scene_mod.SceneInitializer().params

        collected: list[str] = []
        for group, entries in params.items():
            if group in ("reservoirs", "agents") or not isinstance(entries, list):
                continue
            collected.extend(str(a.name) for a in entries)
        supply_to_reservoir = {a.tp: a.name for a in params["reservoirs"]}
        needed = {a.tp for a in params["fires"]}
        collected.extend(
            str(supply_to_reservoir[tp]) for tp in needed if tp in supply_to_reservoir
        )
        targets = sorted(set(collected)) or None
    except Exception:  # noqa: BLE001
        # 场景文件缺失 / 结构变动 / import 失败：一律降级为"无从判定"。
        # 这是**纯测量增项**，不能因为复算失败就让整个 grader 崩掉。
        targets = None

    _COVERAGE_TARGET_CACHE[scene_num] = targets
    return targets


def compute_coverage_verified(episode: EpisodeDataset) -> float | None:
    """成功感知的覆盖率 —— 只有**成功**的交互才算覆盖到目标对象。

    与 `coverage`（论文口径）的唯一差别：这里要求动作 `succeeded is True`。

    为什么要另立一个指标而不是修 `check_coverage`：论文**正文**把 Coverage
    定义为"与目标对象**成功**交互的比例"，但论文作者的参考实现（以及本项目
    照搬的 `SAR/Scenes/base_checker.py:check_coverage`）只做动作**文本**子串
    匹配、完全不看 success —— 论文 Table 7 的已发布数字就是这个
    success-agnostic 版本产出的。直接改旧口径会同时打断（a）与论文数字的
    可比性、（b）与 `sar_orch/results/` 下上百个既有 eval_report.json 的
    可比性。所以旧口径**原样不动**，新口径并列新增。

    这条口径存在的实际动机：旧口径可被"在动作文本里念出目标名字"零成本刷高
    —— 两次**失败**的 `NavigateTo(CaldorFire)` 就能把 coverage 抬到 2/N，
    而 `coverage_mean` 是当前受门禁约束的指标。对自进化循环来说这是最便宜的
    伪改进通道，必须有一个不可如此刷高的并列口径来暴露它。

    数据源用 `agent_interactions.csv`（逐条交互）而非 `trajectory.csv` 的
    Actions 列：后者每个 (step, agent) 只留**一行代表动作**，而同一步里
    agent 可能提交过多个环境动作，checker 是**每个**都过一遍的。实测 87 个
    真实 run，用交互流复算旧口径可与已记录的 Coverage 精确对上 82 个
    （余下 5 个是日志本身不全，双向都有偏差，与算法无关）。

    返回 None 表示"无从判定"（scene 未知或复算不出目标名单），不是 0.0。
    """
    targets = coverage_targets_for_scene(episode.metadata.get("scene"))
    if not targets:
        return None

    verified: set[str] = set()
    for step_num in sorted(episode.steps.keys()):
        for ai in episode.steps[step_num].interactions:
            # 只看真正提交给引擎的动作；工具调用（map_agent__* /
            # report_observation / get_skill ...）的文本里也会出现目标名字，
            # 但它们从未经过 checker，计入会凭空抬高覆盖率。
            if ai.action_name not in _ENV_ACTION_NAMES:
                continue
            if ai.succeeded is not True:
                continue
            for obj in targets:
                if obj in ai.action:
                    verified.add(obj)
    return len(verified) / len(targets)


def grade_outcome(episode: EpisodeDataset) -> list[GradeResult]:
    results: list[GradeResult] = []

    last = episode.last_step
    if last is None:
        return results

    final_coverage = last.coverage
    # 并列的成功感知口径（不替代 final_coverage，见 compute_coverage_verified）。
    coverage_verified = compute_coverage_verified(episode)
    final_transport_rate = last.transport_rate
    finished = last.finished
    total_steps = max(episode.steps.keys()) if episode.steps else 0
    end_reason = last.end_reason

    # 协调器下发的 dispatch 计数（来自 subtasks.csv）。
    # 注意：这**不是**环境 checker 的子任务数 —— subtasks.csv 记录的是
    # coordinator 分派给 worker 的自然语言任务（scene 1 两个 agent 各 1 条，
    # 共 2 条），而 checker.subtasks 是 15 个可判定的原子子任务。
    # 论文 TR 的分母是后者；这里两个字段仅用于观测调度行为，不参与效率计算。
    dispatch_completed_count = sum(
        1 for s in episode.subtask_records if s.status == "completed"
    )
    dispatch_count = len(episode.subtask_records)

    # token aggregation
    token_usage_rows = episode.token_usage_rows
    total_tokens = 0
    agent_tokens: dict[str, int] = {}
    map_tokens = 0
    map_agent_tokens = 0
    map_summarizer_tokens = 0
    for row in token_usage_rows:
        agent = row.get("Agent", "")
        tt = int(row.get("TotalTokens", 0))
        total_tokens += tt
        agent_tokens[agent] = agent_tokens.get(agent, 0) + tt
    map_agent_tokens = agent_tokens.get("MapAgent", 0)
    map_summarizer_tokens = agent_tokens.get("MapSummarizer", 0)
    map_tokens = map_agent_tokens + map_summarizer_tokens

    # 环境 checker 真实判定完成的子任务数（trajectory.csv 的
    # CompletedSubtasksDelta 逐步累加）。这是唯一与论文 TR 同源的完成量：
    # TR == trajectory_completed / len(checker.subtasks)。
    trajectory_completed = sum(
        len(sr.completed_subtasks_delta) for sr in episode.steps.values()
    )

    # checker 的子任务总数（论文 TR 的分母）。它没有被任何产物直接记录，
    # 但 TR == trajectory_completed / total 成立，故可由二者反推。
    # TR 为 0 时无法反推，返回 None 而不是猜一个数。
    checker_subtask_total: int | None = None
    if final_transport_rate and trajectory_completed:
        checker_subtask_total = round(trajectory_completed / final_transport_rate)

    # 每步产出的已完成子任务数。分子必须用 checker 的真实完成量，
    # 不能用 total_subtasks —— 后者是协调器下发的 dispatch 条数（见下方
    # dispatch_count），与环境子任务是两个不同概念，用它会把本指标压成
    # 一个与"效率"无关的数（例如 2/30 而非 13/30）。
    step_efficiency = trajectory_completed / total_steps if total_steps > 0 else 0.0

    # 每完成一个子任务平均消耗的 token（越低越好）。分母与 step_efficiency
    # 的分子同源，保证两个效率指标口径一致。
    token_efficiency = (
        total_tokens / trajectory_completed if trajectory_completed > 0 else 0.0
    )

    # map overhead ratio
    map_overhead = map_tokens / total_tokens if total_tokens > 0 else 0.0

    # balance —— 公式不动（论文 §5 定义，改了就不是论文指标），但它已降级为
    # 诊断量，不再参与门禁判定（审计：与成功反相关）。原值保留以保论文可比性。
    balance = _compute_balance(episode)

    # idle_ratio —— balance 的替代诊断量，衡量真正的浪费而非分工。
    idle_ratio = compute_idle_ratio(episode)

    # 逐工具行为计数 —— DESIGN 3.1b 指定的改进证据来源（SR 只作不退步约束）。
    tool_outcomes = compute_tool_outcomes(episode)
    timeout_steps = compute_timeout_steps(episode)

    # rescue-level 完成口径 —— 取代 tool_outcomes["drop_off_person"] 的原始
    # 成功率作为改进证据：那个口径对"改进"和"协议内合理重试"不可辨识
    # （见 compute_rescue_outcomes 文档）。rescue_summary 为 None 表示本 run
    # 无 drop_off_person 事件，应被排除出跨 run 统计而非记 0。
    rescue_outcomes = compute_rescue_outcomes(episode)
    rescue_summary = compute_rescue_summary(episode)
    rescue_transport_consistency = check_rescue_transport_consistency(episode)

    # progress curve data from CompletedSubtasksDelta
    progress_curve = []
    cumulative = 0
    for step_num in sorted(episode.steps.keys()):
        sr = episode.steps[step_num]
        delta = sr.completed_subtasks_delta
        cumulative += len(delta)
        progress_curve.append(
            {"step": step_num, "delta": len(delta), "cumulative": cumulative}
        )

    episode_result = GradeResult(
        grader="OutcomeGrader",
        level="episode",
        passed=None,
        score=None,
        detail={
            "final_coverage": final_coverage,
            # 成功感知覆盖率（新增并列口径，非论文口径）。None = 无从判定。
            "coverage_verified": coverage_verified,
            "final_transport_rate": final_transport_rate,
            # 与 final_transport_rate 同值的正名字段：TR 这个名字暗示"运输率"，
            # 实际是 checker 子任务完成率，且分母因 SAR/Scenes/checker.py:117
            # 的 list(set(...)) 去重而随场景布局变化。旧名保留（论文可比 +
            # 向后兼容），新名用于让消费者读到正确语义。
            "subtask_completion_rate": final_transport_rate,
            "finished": finished,
            "total_steps": total_steps,
            "end_reason": end_reason,
            # 环境 checker 的真实完成量与总数（与论文 TR 同源）
            "completed_subtasks_trajectory": trajectory_completed,
            "checker_subtask_total": checker_subtask_total,
            # 协调器 dispatch 计数，非环境子任务 —— 勿用于效率/完成率
            "dispatch_count": dispatch_count,
            "dispatch_completed_count": dispatch_completed_count,
            "step_efficiency": step_efficiency,
            "total_tokens": total_tokens,
            "agent_tokens": agent_tokens,
            "map_tokens": map_tokens,
            "map_agent_tokens": map_agent_tokens,
            "map_summarizer_tokens": map_summarizer_tokens,
            "map_overhead_ratio": map_overhead,
            "token_efficiency": token_efficiency,
            "balance": balance,
            "idle_ratio": idle_ratio,
            "tool_outcomes": tool_outcomes,
            "rescue_outcomes": rescue_outcomes,
            "rescue_summary": rescue_summary,
            "rescue_transport_consistency": rescue_transport_consistency,
            "timeout_steps": timeout_steps,
            "progress_curve": progress_curve,
        },
        evidence_ref="trajectory.csv,summary.csv,token_usage.csv,subtasks.csv",
    )
    results.append(episode_result)

    return results


OUTCOME_GRADER_NAME = "OutcomeGrader"

# 2 形式模型与不变式

## 2.1 建模目的与层次

原始 LLaMAR 已将部分可观测多机器人长时程任务表述为 POMDP，回答了“环境中有哪些 agent、观测、动作、转移与任务目标”这一问题；但它并未显式区分 LLM 产生的计划建议、框架持有的执行状态以及通信授权状态。对于当前重构而言，后者正是需要可审计的对象。

因此，本章采用两层模型：

$$
\underbrace{\langle N,\mathcal I,\mathcal S,\{\mathcal O_i\}_{i=1}^{N},
\{\mathcal A_i\}_{i=1}^{N},\mathcal P,\mathcal G,T\rangle}_{\text{环境与任务层（继承原始 LLaMAR）}}
\quad \oplus \quad
\underbrace{\langle G_m,D,\Pi,\Lambda\rangle}_{\text{协调运行时层（本文实现化）}}.
$$

符号 $\oplus$ 表示协调层附着于环境层的执行过程之上，而非用任务图替换 POMDP 的物理状态。这一区分可避免两类常见误读：其一，MissionGraph 不等同于环境真实状态；其二，框架约束并不替代 LLM 对未知环境、资源状态或策略的推理。

## 2.2 底层：继承的 POMDP 与 SAR 映射

### 2.2.1 原始 POMDP 定义

原始 LLaMAR 将多机器人长时程任务形式化为 POMDP，定义为八元组【Nayak et al.：§3】：

$$
\mathcal E = \langle N,\mathcal I,\mathcal S,\{\mathcal O_i\},\{\mathcal A_i\},\mathcal P,\mathcal G,T\rangle,
$$

各分量的含义如下：

- $N$：agent 数量。每个 agent 独立感知环境并执行动作，但它们共享同一个联合状态空间。
- $\mathcal I$：高层语言指令集合。任务以自然语言给出（如"扑灭所有火灾并救出失踪人员"），agent 需要将其分解为可执行的子任务。
- $s_t \in \mathcal S$：时刻 $t$ 的联合环境状态，包含所有 agent 的位置、所有火场的强度与范围、所有人员的位置与状态、所有资源的分布等。**单个 agent 无法直接观测到完整的 $s_t$**——这正是"部分可观测"的含义。
- $o_t^i \in \mathcal O_i$：agent $i$ 在时刻 $t$ 的局部观测。它只包含该 agent 视野范围内的信息（相邻格子的内容、全局可见对象的名称/类型/强度），不包括其他 agent 的观察或完整环境状态。
- $\mathcal A = \mathcal A_{NAV} \cup \mathcal A_{INT} \cup \mathcal A_{EXP}$：联合高层动作空间，分为三类。$\mathcal A_{NAV}$ 是导航动作（如 `NavigateTo(targetID)`），$\mathcal A_{INT}$ 是交互动作（如 `GetSupply`、`UseSupply`、`Carry`），$\mathcal A_{EXP}$ 是探索动作。所有 agent 在每个高层决策步同步执行各自选择的动作。
- $\mathcal P(s_{t+1} \mid s_t, a_t)$：联合转移概率函数，描述在状态 $s_t$ 下执行联合动作 $a_t$ 后到达 $s_{t+1}$ 的概率。火灾蔓延、资源消耗、人员搬运等动态过程都由 $\mathcal P$ 刻画。
- $\mathcal G = \{g_1, \cdots, g_k\}$：目标所需子任务集合。每个子任务对应一个可判定的完成条件（如"扑灭火灾 X""将人员 Y 运送到存放点"），全部完成后任务即告成功。
- $T$：规划视界（最大步数）。agent 必须在 $T$ 步内完成所有子任务，否则任务失败。

这个 POMDP 模型回答了"环境中有什么、agent 能看到什么、能做什么、环境如何演变、目标是什么、有多少时间"这六个基本问题。但它只描述了**环境层**——LLM 的计划建议、框架持有的执行状态、以及通信授权状态，并不在这个模型的表达范围内。对于当前重构而言，后者正是需要可审计的对象。

### 2.2.2 SAR 场景中的具体对应

在 SAR（搜索与救援）场景中，上述抽象分量对应以下具体实体【Nayak et al.：附录 D】：

**环境与任务**：多个 agent 在一个未知网格环境中协作，需要扑灭多处火灾（A 类用水、B 类用沙）并将失踪人员运送到指定存放点。火灾会随时间蔓延，强度越高蔓延越快；搬运人员需要至少 2 个 agent 同时协作。任务的核心挑战在于：灭火需要先识别火源类型、再获取对应资源、最后使用资源，形成显式的依赖链；探索与灭火之间存在资源分配权衡；且火势蔓延使得当前决策具有不可逆的长期后果。

**记号与代码的对应关系**：

| 形式对象 | 当前 SAR 实现 | 解释与边界 |
|---|---|---|
| $\mathcal O_i$ | `SAREnv.generate_obs_text(agent_idx)`，由 barrier 每回合写回 | 每个 Worker 接收局部文本观测；Coordinator 的 semantic state projection 是额外框架视图，不等于 $o_i$ |
| $\mathcal A$ | `Controller.ALL_ACTIONS + ['Explore']` | 高层动作经领域工具提交；零成本查询/汇报工具不等同于环境动作 |
| $\mathcal G$ | `Checker.initialize(params)` 生成的 checker 子任务集 | 每火灾、人员和必要水库产生可计分条目【`SAR/Scenes/checker.py:27–121`】 |
| $\mathcal P$ | `SARBarrier` 聚合联合动作后调用 `SAREnv.step()` | 当前实现以确定性仿真执行一次回合；形式上的概率核保留原论文一般性 |
| $T$ | `run_experiment(..., max_steps=...)` 的步数预算 | 默认取场景 `task_timeout`；论文实验是否采用统一预算，须在实验章节单独冻结 |

### 2.2.3 本文在 POMDP 基础上的扩展

原始 POMDP 是本文的共享基座，不应被表述为新贡献。本文的新增内容是：在一个环境回合前后，如何限制**逻辑节点、物理派发和通信成员关系**的合法转换。

### 2.2.4 LLM 决策函数：从动态上下文到工具调用

POMDP 中的策略通常写为 $a_t^i\sim\pi_i(\cdot\mid o_t^i)$，即 agent 在时刻 $t$ 根据局部观测直接选择一个动作。对当前 LLM Agent 而言，这一写法过于简化：模型并非只读取一次局部观测就直接输出动作，而是在每个环境步内可能经历多轮推理——每轮推理的上下文都可能不同（因为工具执行结果会更新消息历史和运行时状态），直到模型决定提交一个环境动作为止。

为刻画这一过程，先定义第 $t$ 个环境步中 agent $i$ 第 $k$ 轮调用前的内部决策状态：

$$
z_{i,t,k}=
\bigl\langle
\bar p_i,\ o_t^i,\ H_{i,t,k},\ C_{i,t,k},\ \mathcal T_{i,t,k};\ \eta_i
\bigr\rangle,
\qquad
z_{i,t,1}=\operatorname{Init}_i(\bar p_i,o_t^i,\eta_i).
\tag{1}
$$

其中 $\bar p_i$ 是有效 system prompt，$o_t^i$ 是本环境步开始时获得的局部观测，$H_{i,t,k}$、$C_{i,t,k}$ 与 $\mathcal T_{i,t,k}$ 分别是当前消息历史、动态 Context Memory 和 LLM 可见工具集合。由此，第 $k$ 轮模型输出是条件于该轮决策状态的随机变量：

$$
y_{i,t,k}\sim \pi_{\theta_i}\!\left(
    \cdot\ \middle|
    \operatorname{Assemble}\bigl(\bar p_i,o_t^i,H_{i,t,k},C_{i,t,k}\bigr),
    \mathcal T_{i,t,k};\ \eta_i
\right),
\tag{2}
$$

其中 $y_{i,t,k}$ 为第 $k$ 轮的模型输出（自然语言、tool call 或二者兼有），$\theta_i$ 为该 agent 本轮运行所使用的冻结模型权重/模型版本，$\eta_i$ 表示解码配置（如 temperature、token 上限和 provider 参数），$\mathcal T_{i,t,k}$ 是当前注册且对 LLM 可见的工具集合。`Agent.run()` 将组装后的 `messages` 和 `tool_list` 同时传给 `LLM.generate()`【`src/Agent/router_agent/agent.py:506–522`】；工具集合由 builtin、custom 和 runtime `extra_tools` 合并而成，其描述会附加到 system prompt【`src/Agent/router_agent/build.py:120–194`】。

这里 $\bar p_i$ 并非裸 prompt，而是已与工具描述、可选 skill 元数据合并后的有效 system prompt；$H_{i,t,k}$ 是第 $k$ 轮推理时的原始消息历史；$C_{i,t,k}$ 是 ContextManager 生成的动态 memory block。实际组装顺序为"system prompt → 原始消息 → memory block"，其中 memory block 作为最后一条 user message 附加，以保留稳定前缀并兼顾缓存效率【`src/Agent/router_agent/context.py:629–649`】。

为展开 $C_{i,t,k}$，定义：

$$
C_{i,t,k}=
\operatorname{Render}\left(
    Z_{i,t,k}^{\mathrm{pin}},
    Z_{i,t,k}^{\mathrm{hist}},
    R_{i,t,k},
    \Omega_i
\right),
\tag{3}
$$

其中：

| 变量 | 实现对应 | 决策作用与边界 |
|---|---|---|
| $Z_{i,t,k}^{\mathrm{pin}}$ | typed pinned state / `pinned` | 结构化的当前位置、库存、任务状态、步数预算、已知对象等；由工具结果和运行时状态投影更新 |
| $Z_{i,t,k}^{\mathrm{hist}}$ | 原始 assistant/tool messages 与压缩/episodic 历史 | 保留最近交互及必要的历史摘要；其长度受 ContextConfig 与 token 预算约束 |
| $R_{i,t,k}$ | `StateProvider.snapshot()` 的 `RuntimeState` | 框架在每次调用前读取的动态运行时投影，而非模型自身推断出的真相 |
| $\Omega_i$ | ContextConfig、输出格式及 agent 角色配置 | 控制 `semantic/oracle` 模式、recent window、pinned state 和渲染格式、上下文压缩策略 |

在默认 `semantic` 模式下，Coordinator 的 $R_{i,t,k}$ 进一步包含由语义地图构造的环境记忆：

$$
R_{\mathrm{coord},t,k}^{\mathrm{semantic}}
=\Phi\!\left(
    M_{t,k}^{\mathrm{map}},\Delta M_{t,k},\bar M_{t,k},
    G_{m,t},D_t,\Pi_t,B_t,Q_t
\right),
\tag{4}
$$

其中 $M_{t,k}^{\mathrm{map}}$ 是 `SemanticMapStore` 合并的、来自 Worker 上报观测的环境共识；$\Delta M_{t,k}$ 与 $\bar M_{t,k}$ 分别是地图差分和可选增量摘要；$G_{m,t},D_t,\Pi_t$ 是本章定义的任务图、物理派发与通信分区；$B_t$ 为步数预算，$Q_t$ 为任务/监督状态。`SARCoordinatorStateProvider` 在 semantic 模式下将这些字段投影进 `RuntimeState.payload`，随后由 ContextManager 写入 pinned state 并渲染为 Context Memory【`sar_orch/coordinator_state_provider.py:186–294`；`src/Agent/router_agent/context.py:187–225,726–750`】。因此，**map memory 并非独立绕过 ContextManager 直接喂给模型的通道**，而是沿 `SemanticMapStore → StateProvider → ContextManager → memory block` 这一受控数据流传递。

模型输出通过受工具契约限制的解码函数映射为可执行决策：

$$
d_{i,t,k}=\operatorname{Decode}_{\mathcal T_{i,t,k}}(y_{i,t,k})
=
\begin{cases}
(\tau,\mathrm{args}), & \text{若 } y_{i,t,k}\text{ 含合法工具调用 }\tau\in\mathcal T_{i,t,k},\\
\texttt{final\_text}, & \text{若无工具调用且满足结束条件},\\
\texttt{invalid}, & \text{若输出不能通过协议/工具解析}.
\end{cases}
\tag{5}
$$

当 $d_{i,t,k}=(\tau,\mathrm{args})$ 时，框架执行工具、获得结果 $e_{i,t,k}$，并更新消息历史、pinned state、语义地图或协调运行时状态；下一轮推理形成新的决策状态。将这一状态转移写为：

$$
e_{i,t,k}=\operatorname{Exec}_i(d_{i,t,k}),
\qquad
z_{i,t,k+1}=\operatorname{Update}_i
\bigl(z_{i,t,k},y_{i,t,k},d_{i,t,k},e_{i,t,k}\bigr).
\tag{6}
$$

令 $\operatorname{Commit}_i(d)=1$ 表示解码结果 $d$ 已通过当前工具契约和运行时校验、可提交为一个环境动作。内部推理在首次满足该条件时停止：

$$
K_{i,t}=\inf\bigl\{k\geq 1:\operatorname{Commit}_i(d_{i,t,k})=1\bigr\}.
\tag{7}
$$

定义该环境步的完整内部决策轨迹为

$$
\xi_{i,t}=
\bigl(z_{i,t,1},y_{i,t,1},d_{i,t,1},e_{i,t,1},\ldots,
z_{i,t,K_{i,t}},y_{i,t,K_{i,t}},d_{i,t,K_{i,t}}\bigr).
\tag{8}
$$

于是，agent $i$ 在第 $t$ 步提交的环境动作应表述为整段内部轨迹的解码结果，而非仅由初始局部观测或孤立的一次模型调用决定：

$$
a_t^i=\operatorname{CommitAction}_i\bigl(\xi_{i,t}\bigr).
\tag{9}
$$

在通常的“末轮合法工具调用即提交动作”的实现中，$\operatorname{CommitAction}_i$ 可退化为从 $d_{i,t,K_{i,t}}$ 提取动作参数；但该末轮输出本身已条件于此前所有 $y_{i,t,1:K_{i,t}-1}$、工具结果和状态更新。因此，环境层的策略应理解为由多轮决策轨迹诱导的策略：

$$
\pi^{\mathrm{ind}}_{\theta_i,\eta_i}(a\mid o_t^i)
=
\sum_{K=1}^{\infty}
\int
\mathbf{1}\!\left[
\operatorname{CommitAction}_i(\xi_{i,t})=a
\right]
p_{\theta_i,\eta_i}(\xi_{i,t}\mid o_t^i)
\,d\xi_{i,t},
\qquad
a_t^i\sim\pi^{\mathrm{ind}}_{\theta_i,\eta_i}(\cdot\mid o_t^i).
\tag{10}
$$

式（10）将所有可能的内部轮数、模型输出、工具反馈和运行时更新轨迹边缘化；它保留了 POMDP 在环境步粒度上的动作接口，同时明确该动作由多轮 LLM--工具闭环共同产生。

该式刻画的是**推理时上下文与工具反馈的闭环**，而非参数学习过程：在一次实验内，ContextManager、工具结果和 map memory 改变的是 $H/Z/R$，不改变 $\theta_i$。若未来引入 fine-tuning、RL 或自进化 scaffold 更新，才需要额外定义 $\theta_{i,k+1}$ 或 prompt/topology 的跨任务更新方程；当前实现不应被表述为更新模型权重的自进化系统。

## 2.3 协调运行时状态

令 $W_t$ 为时刻 $t$ 在线 Worker 集合。协调运行时记为：

$$
\mathcal R_t=\langle G_{m,t},D_t,\Pi_t,\Lambda_t\rangle.
$$

其中 $G_{m,t}$ 是逻辑 Mission 图，$D_t$ 是物理 dispatch 记录，$\Pi_t$ 是 team assignment，$\Lambda_t$ 是 mission admission lease。环境状态 $s_t$ 与协调状态 $\mathcal R_t$ 通过任务执行、回调与终止过程相互作用，但二者的真相来源不同。

### 2.3.1 逻辑图 $G_m$

定义逻辑 MissionGraph：

$$
G_m=(V,E,\nu), \qquad E\subseteq V\times V,
$$

其中 $V$ 为逻辑节点集合，$E$ 为依赖边，$\nu(v)$ 包含节点的参与者、目标、分配和运行态。对任一节点 $v$：

$$
\nu(v)=\langle id(v),P(v),q(v),A(v),pred(v),\ell(v),\sigma(v)\rangle,
$$

其中 $P(v)\subseteq W_t$ 是非空且无重复的参与者集合，$q(v)$ 是 objective，$A(v)$ 是按 Worker 的 assignment，$pred(v)$ 是前驱集合，$\ell(v)\in\{\texttt{pending},\texttt{skipped}\}$ 是声明态，$\sigma(v)$ 是框架持有的运行态。当前实现要求 ID 唯一、依赖指向图内节点、无自环且无环【`src/a2a/coordinator/mission_graph.py:206–247`】。

运行态集合为：

$$
\Sigma_L=\{\texttt{planned},\texttt{blocked},\texttt{ready},\texttt{activating},
\texttt{active},\texttt{completed},\texttt{failed},\texttt{canceled}\}.
$$

`completed/failed/canceled` 是终态。依赖 frontier 定义如下：

$$
\operatorname{ready}(v) \iff \ell(v)\neq\texttt{skipped}\ \land\
\bigl(pred(v)=\varnothing\ \lor\ \forall u\in pred(v),\operatorname{succ}(u)\bigr),
$$

其中 $\operatorname{succ}(u)$ 在当前实现中表示 $u$ 已 `completed` 或其声明态为 `skipped`【`mission_graph.py:643–665`】。因此，`failed` 与 `canceled` 前驱不会放行后继；这是一条显式的失败传播语义，而非由 LLM 自行解释依赖文本。

### 2.3.2 物理记录 $D$

每个逻辑节点为每个参与者展开一条物理 dispatch：

$$
D(v)=\{d_{v,w}\mid w\in P(v)\},\qquad D=\bigcup_{v\in V}D(v).
$$

每个 $d\in D$ 的核心字段为：

$$
d=\langle id_d, id_v,w,id_{task},\sigma_D,artifact,result,seq\rangle,
$$

其中 $id_d=\texttt{dsp\_uuid}$ 是框架生成的 opaque ID，$id_v$ 是逻辑节点 ID，$id_{task}$ 为 Worker A2A task ID，$seq$ 是终态写入序号。定义逻辑与物理 ID 空间：

$$
V_{id}\cap D_{id}=\varnothing.
$$

这并非概率意义上的 UUID 碰撞分析，而是 API 语义约束：逻辑图 API 只解释 logical ID，dispatch 查询只解释 `dispatch_id`，不会以另一个 namespace 作为回退【`mission_runtime.py:220–287,311–317`】。

物理状态集合为：

$$
\Sigma_D=\{P,Di,A,R,I,C_p,C,F,X\},
$$

分别对应 `PREPARED`、`DISPATCHING`、`ACCEPTED`、`RUNNING`、`INPUT_REQUIRED`、`CANCEL_PENDING`、`COMPLETED`、`FAILED`、`CANCELED`。允许边由 `_ALLOWED_TRANSITIONS` 显式限定，终态 $\{C,F,X\}$ 没有出边【`mission_runtime.py:37–106`】。对一条状态更新 $u=(id_d,s,source,t)$，只有当 $s$ 是当前状态的允许后继时才改变 $\sigma_D$；重复、未知或过晚的状态只产生 diagnostic，不能回写新的终态【`mission_runtime.py:451–532`】。

逻辑聚合定义为：

$$
\sigma(v)=
\begin{cases}
\texttt{failed}, & \exists d\in D(v):\sigma_D(d)\in\{F,X\},\\
\texttt{completed}, & \forall d\in D(v):\sigma_D(d)=C,\\
\texttt{active}, & \text{其他非终态情形}.
\end{cases}
$$

该聚合只接受 canonical 物理终态；一封 MAIL、一次 artifact 更新或单个 Worker 的自然语言结果，都不构成逻辑完成的依据【`mission_graph.py:543–590`】。


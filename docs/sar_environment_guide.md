---
日期: 2026-06-19
文档类型: 技术手册 / 代码阅读指南
文档概述: LLaMAR 项目中 Search & Rescue (SAR) 仿真环境的完整架构解析，从底层引擎到高层 LLM 集成
---

# SAR 仿真环境 — 完整架构指南

## 一、概述

**SAR (Search & Rescue)** 是 LLaMAR 框架中的两个实验领域之一，一个**自定义 grid-world 仿真引擎**，模拟多智能体在部分可观测环境中的协作搜索与救援任务。

### 核心玩法

在一个 **30×30** 的网格世界上（可扩展），2–6 个机器人智能体需要协作完成：

- 🔥 **灭火** — 识别火灾类型（化学火/非化学火），获取对应资源（沙/水），在火势蔓延前扑灭
- 🧑 **搜救人员** — 探索环境找到失踪人员，≥2 个智能体协作搬运到安全存放点

### 与 AI2-THOR 的对比

| 维度 | SAR | AI2-THOR (MAP-THOR) |
|------|-----|---------------------|
| 环境 | 自定义 grid-world（纯 Python） | 3D 室内模拟器（Unity） |
| 智能体 | 2–6 个 | 2 个（Alice & Bob） |
| 动作 | 移动、取/用资源、搬运 | 导航、物体交互 |
| 观测 | 文本描述（网格局部 + 全局） | 视觉图像 + 文本 |
| 状态空间 | 离散网格坐标 | 连续 3D 空间 |
| 依赖 | 纯 Python（无外部引擎） | ai2thor Unity 二进制 |

### 文件结构

```text
SAR/
├── core.py                        # ★ 核心引擎 (2572行) — 所有对象类 + 控制器
├── env.py                         # SAREnv — 高层环境封装，LLM 接口
├── base_env.py                    # SARBaseEnv — 动作解析、文本生成、探索
├── object_actions.py              # 语义相似度动作匹配（Sentence-BERT）
├── misc.py                        # 工具函数（Arg, 缓存装饰器, 枚举工具）
├── utils.py                       # matplotlib 网格渲染器
│
├── Scenes/
│   ├── scene_initializer.py       # BaseSceneInitializer — 场景验证 + 控制器创建
│   ├── get_scene_init.py          # 场景工厂（编号 → 模块动态导入）
│   ├── scene_{1..5}.py            # 5 个预设场景定义
│   ├── checker.py                 # Checker — 任务完成度检查
│   ├── base_checker.py            # BaseChecker — 子任务和覆盖追踪框架
│   └── render_scene.py            # 可视化测试脚本
│
└── baselines/
    ├── llamar.py                  # ★ 主入口 — Planner→Actor→Verifier 循环
    ├── llamar_utils_multiagent.py # LLM 调用、Prompt、动作后处理
    └── sar_logging.py             # 实验日志记录
```

---

## 二、架构分层

SAR 是一个**四层架构**，从上到下：

```
  LLM Agent Loop (llamar.py)
         ↕ NL 字符串接口
  ┌─────────────────────────────────────────────────────┐
  │  第1层: SAREnv (env.py)                              │
  │  高层封装：LLM 输入生成、状态追踪、失败历史、探索动作    │
  │  接口: step(actions: List[str]) → (obs_str, successes)│
  └──────────────────────┬──────────────────────────────┘
                         ↓
  ┌─────────────────────────────────────────────────────┐
  │  第2层: Controller (core.py)                         │
  │  动作调度：分发智能体动作、生成观测、驱动仿真时钟       │
  │  接口: step(action, **kwargs) → event dict            │
  └──────────────────────┬──────────────────────────────┘
                         ↓
  ┌─────────────────────────────────────────────────────┐
  │  第3层: Field + Backend + GridEngine (core.py)       │
  │  世界状态：所有 POI 容器、可见性计算、碰撞检测          │
  └────┬──────────────┬──────────────┬──────────────────┘
       ↓              ↓              ↓
  ┌────────┐  ┌────────────┐  ┌──────────────┐
  │ POI    │  │ Fire       │  │ Person       │
  │ 对象   │  │ 蔓延引擎    │  │ 耦合系统     │
  │ (装饰器)│  │ 灭火逻辑    │  │ 搬运协议     │
  └────────┘  └────────────┘  └──────────────┘
       ↓
  ┌────────┐
  │ GPS    │  ← 全局位置注册表（静态类）
  └────────┘
```

---

## 三、核心引擎 — `core.py` (2572 行)

### 3.1 装饰器体系（Python 类包装模式）

所有世界对象通过**装饰器组合**构建，而非继承：

```python
@collidable(is_collidable=False)    # 最外层包装
@with_id                             # 添加唯一 UUID
@with_position(mutable=False)        # 添加坐标 + 可见性半径
@named                               # 最内层包装：添加人类可读名称
class Flammable: ...                 # 原始类
```

| 装饰器 | 参数 | 添加的能力 |
|--------|------|-----------|
| `@named` | — | `name` 属性, `set_name()`/`get_name()` |
| `@with_id` | — | `id` (UUID), `class_name()` 自省 |
| `@with_position(mutable)` | `mutable: bool` | `position` (Coordinate), 可见性 `sees()`, GPS 跟踪 |
| `@collidable(is_collidable)` | `is_collidable: bool` | `collidable` 类属性，控制是否阻挡移动 |

**MRO 示例**（以 Flammable 为例）：

```
CollidableWrapper(IdWrapper(PositionWrapper(NameWrapper(Flammable))))
```

### 3.2 对象类一览

| 类 | 装饰器 | 可碰撞 | 可变位置 | 关键属性 | 用途 |
|----|--------|--------|---------|---------|------|
| `Flammable` | 全部 4 个 | ❌ | ❌ | `fire_type`, `intensity`, `_clock` | 单个可燃格子 |
| `Fire` | 3 个（无 @named 外层） | ❌ | ❌ | `flammables[]`, `dneighboor`, `impotent` | 聚合多个 Flammable |
| `Reservoir` | 3 个 | ✅ | ❌ | `type`, `left` (无限) | 资源源头 |
| `Deposit` | 3 个 | ✅ | ❌ | `capacity`, `storage{}` | 资源/人员存放点 |
| `Person` | 全部 4 个 | ✅ | ✅ | `coupled{}`, `extraload`, `deposited` | 待救援人员 |
| `AbsAgent` | 全部 4 个 | ✅ | ✅ | `inventory{}` | 机器人智能体 |

### 3.3 火势系统

**单格状态机：**

```text
NONE ──[light()]──▶ LOW ──[clock≥3]──▶ MEDIUM ──[clock≥6]──▶ HIGH
  ▲                    │                    │                    │
  └──[extinguish()]────┘──[lessen(匹配类型)]─┘──[lessen(匹配类型)]─┘
```

- **LOW→MEDIUM**: 3 个仿真时钟 tick
- **MEDIUM→HIGH**: 再 3 个 tick（总共 6）
- **蔓延条件**: 强度 ≥ MEDIUM，向 8 方向 Moore 邻域点燃
- **"无能态" (impotent)**: 火初始不蔓延，直到被智能体发现才激活 → 给智能体准备时间
- **Tick 控制**: `Fire.STEP_EVERY=2`，每 2 个全局 tick 执行一次蔓延
- **类型系统**: `A=化学火/需沙`，`B=非化学火/需水`，类型不匹配灭火无效
- **灭火溅射**: `Fire.lessen(loc, type, diagonal=True)` 除目标格还影响邻域

### 3.4 人员救援系统

**耦合协议 — 多智能体协同的关键：**

```
Person.pick(agent)  → agent 加入 coupled 字典
Person.drop(agent, deposit) → agent 加入 uncoupled 字典
```

- **最低负载**: `MIN_REQUIRED_AGENTS=2` + `extraload`（默认 0）
- **只有 `len(coupled) ≥ load`** 时人员才算被"抓住"(GRABBED)
- **成功投放条件**（三者同时满足）：
  1. `grabbed` — 足够智能体耦合
  2. `depositable` — 所有耦合智能体都在存放点半径内
  3. `all_dropped` — 所有耦合智能体都调用了 `drop()`
- **跟随后**: `Person.step()` 跟随第一个耦合智能体的位置
- **可见性特殊规则**：
  - 默认：仅在智能体视野半径内可见
  - **一旦任一智能体发现 → `spotted=True` → 对所有智能体永久可见**
  - **一旦投放 → `deposited=True` → 对所有智能体永久不可见**

### 3.5 Coordinate 与 GPS

```python
# 坐标系统
Coordinate.WIDTH=30, Coordinate.HEIGHT=30, Coordinate.ALTITUDE=1
# 距离计算：欧几里得、曼哈顿、邻域判断
Coordinate.manhattan(c1, c2)
Coordinate.neighboors(c1, c2, diagonal=True)  # 8方向
Coordinate.within_radius(c)  # 距中心点的半径检测
```

**GPS** 是一个**全局静态位置注册表**：

```python
GPS.tracker = {(x,y): [obj_id1, obj_id2, ...], ...}
GPS.id_mapping = {obj_id: obj_ref, ...}
```

- `GPS.update_track(obj, prev, future)` — 对象移动时更新
- `GPS.at(pos)` — 查询某位置上的所有对象
- `GPS.near(center, radius)` — 半径内所有对象

### 3.6 Field（世界容器）

```python
Field.map = {
    'fires': [...],
    'reservoirs': [...],
    'deposits': [...],
    'agents': [...],
    'persons': [...],
}
```

- 通过 `procedural_generation(params, seed)` 静态方法从参数构建整个世界
- 维护 `visibility` 矩阵：每个智能体 × 每个 POI 的可见性
- 提供 `partial_observation()` 和 `local_partial_observation()` 返回分层的观测数据

### 3.7 Controller（动作调度器）

**动作空间：**

| 动作 | 参数 | 功能 |
|------|------|------|
| `NoOp` | — | 空操作 |
| `NavigateTo(id)` | 目标对象 ID | 路径查找（A* 风格）到目标附近 |
| `Move(direction)` | 方向字符串 | 单步移动（8方向 + Center） |
| `Carry(id)` | 人员 ID | 耦合智能体到人员 |
| `DropOff(person_id, deposit_id)` | 人员 + 存放点 | 投放人员 |
| `GetSupply(source_id, type)` | 资源源 + 类型 | 获取资源 |
| `UseSupply(fire_id, type)` | 火 + 类型 | 使用资源灭火 |
| `StoreSupply(deposit_id)` | 存放点 ID | 存放库存 |
| `ClearInventory` | — | 清空库存 |

**调度流程：**
1. 检查 `NoOp` 捷径
2. 如果携带人员则限制非移动/搬运动作
3. 检查所有引用的对象 ID 是否在视野中
4. 分发到对应的 `Backend` 方法或直接操作 POI
5. 返回 `event dict`： `{success, global_obs, local_obs, error_type, info}`

**同步时钟：**
- 每个智能体有独立的 tick 计数器
- 所有智能体都执行动作后，`_monolithic_step()` 推进全局仿真时钟
- 然后 `Field.step()` 推动火蔓延和人员跟随

---

## 四、环境封装层

### 4.1 SAREnv (`env.py`)

`SAREnv` 是 LLM 循环直接使用的接口，继承 `SARBaseEnv`。

**初始化流程：**

```python
env = SAREnv(num_agents=2, scene=1, seed=42, save_frames=True)
obs_str = env.reset()
```

`reset()` 内部：
1. 调用 `get_scene_initializer(scene_num)` 动态加载场景模块
2. 调用 `SceneInitializer.preinit(num_agents, names, seed)` → 创建 `Controller`
3. 创建 `Checker`
4. 初始化所有状态追踪变量（步骤计数、历史、失败集、内存、子任务）
5. 构建初始 LLM 观测文本 → 返回格式化字符串

**步进方法：**

```python
obs_str, successes = env.step(["NavigateTo(GreatFire)", "Explore()"])
```

`step(actions: List[str])` 对每个智能体：
1. 如果是 `SendMessage` → 直接标记成功（LLM 间通信）
2. 调用 `parse_action()` 将自然语言转 action dict
3. 如果是 `Explore` → 展开为一系列 `Move` 子步骤
4. 调用 `controller.step(**kwargs)` 执行
5. 更新失败集和历史记录
6. 调用 `checker.perform_metric_check()`
7. **DropOff 同步修正**：如果任一智能体 DropOff 成功，所有尝试 DropOff 的都标记成功

### 4.2 SARBaseEnv (`base_env.py`)

**`parse_action(action_string, agent_idx)`** — 将自然语言转为执行字典：

| 输入示例 | 输出字典 |
|---------|---------|
| `"NavigateTo(GreatFire)"` | `{action: 'NavigateTo', to_target_id: obj_id}` |
| `"UseSupply(GreatFire, Sand)"` | `{action: 'UseSupply', to_target_id: fire_id, supply_type: 'Sand'}` |
| `"GetSupply(ReservoirUtah)"` | `{action: 'GetSupply', from_target_id: res_id}` |
| `"Carry(LostPersonTimmy)"` | `{action: 'Carry', from_target_id: person_id}` |
| `"Done"` / `"Idle"` | `{action: 'NoOp'}` |

**`explore_actions(agent_idx)`** — 智能体随机向一个方向探索最多 20 步，除最后一步外均为 `inert_step=True`（不触发完整观测更新）。

**`convert_dict_to_string(input_dict)`** — 将状态字典转换为 LLM 可读的格式化文本。

### 4.3 观测生成

每个智能体获得**分层观测**：

```
Alice's observation:
    Directly around me, I can see:
    {
    Left: ['Obstacle'],
    Right: ['Flammable of fire (intensity Low): GreatFire_Region_1'],
    Up: ['Empty'],
    Down: ['Empty'],
    Center: ['Empty'],
    },
    Globally, I can see: ['GreatFire with average intensity of Low...'],
    Names: ['GreatFire', 'GreatFire_Region_1', 'ReservoirUtah', ...],
```

- **局部观测**: 5 方向（上下左右 + 中心），每个方向列出可见对象
- **全局观测**: 视野半径内的所有对象摘要
- **智能体状态**: 坐标 + 库存 `"I am at (22, 18), holding {Sand: 0, Water: 1}"`

---

## 五、场景系统

### 5.1 场景定义（`Scenes/scene_{1..5}.py`）

每个场景继承 `BaseSceneInitializer`，设置：
- `self.name` — 描述性标识
- `self.params` — 完整世界参数（网格、水库、存放点、火灾、人员、智能体起始位置）
- `self.task_timeout` — 超时步数（全部为 35）

**场景家族：**

| 场景 | 名称 | 火灾 | 火点数 | 人员 | 特点 |
|------|------|------|--------|------|------|
| 1 | `area_900_2_fires_3_lights` | Caldor(A) + Great(B) | 2+1=3 | 1 | 基准场景 |
| 2 | `area_900_two_fires_3_lights` | England(A) + Town(B) | 1+2=3 | 1 | 类型分布互换 |
| 3 | `area_900_3_fires_3_lights` | Ember(A) + Agni(B) + Downtown(B) | 1+1+1=3 | 1 | 3处分散火灾 |
| 4 | `area_900_1_fire_3_lights` | Red(A) | 3 | 1 | 单处大型火灾 |
| 5 | `area_900_1_fire_2_lights_2_persons` | Sussex(B) | 1 | **2** | 多人员协同 |

所有场景共享：**30×30 网格，2 个水库，1 个存放点，6 个预设智能体起始位置**。

### 5.2 初始化流水线

```text
SAREnv.reset()
  └→ get_scene_initializer(scene_num)    # 动态 import scene_N.py
      └→ SceneInitializer()              # 加载 params + task_timeout
          └→ BaseSceneInitializer.check_proper()  # 验证参数完整性
      └→ SceneInitializer.get_task()     # 生成人类可读任务描述
      └→ SceneInitializer.preinit(N, names, seed)
          └→ 裁剪 agents 到前 N 个
          └→ copy.deepcopy(params)
          └→ Controller(pg_params, seed)
              └→ Field.procedural_generation(params, seed)
                  └→ Coordinate.set_params(width, height, altitude)
                  └→ 创建所有 POI 对象
      └→ 后处理：添加 available_object_names，转换类型代码
  └→ Checker(params)                     # 初始化子任务列表
  └→ 初始化所有状态追踪变量
```

### 5.3 任务检查系统

**Checker** 从 `BaseChecker` 继承：

- **`subtasks`** — 所有必须完成的原子任务（去重后）：
  - 每处火：`NavigateTo(FireName)` → `UseSupply(FireName, type)` → `EndFire(FireName)`
  - 每个人员：`NavigateTo(PersonName)` → `Carry(PersonName)` → `DropOff(Deposit, Person)` + `Spot(PersonName)`
  - 每个水库：`GetSupply(ReservoirName)` + `NavigateTo(ReservoirName)`
- **`coverage`** — 必须被观测到的所有对象
- **`callback()`** — 特殊条件检查：`EndFire`（火平均强度==None）、`Spot`（人员被 spotted）
- **`check_success()`** — `len(completed) == len(total)`
- **`get_transport_rate()`** — 进度比例
- **`get_coverage()`** — 覆盖比例

### 5.4 渲染系统

`utils.render()` 使用 **matplotlib** 生成网格可视化：

| 对象类型 | 颜色 |
|---------|------|
| Flammable (NONE→LOW→MED→HIGH) | 白→米黄→黄→橙→蓝 |
| AbsAgent | 粉红 |
| Reservoir A (沙) | 深蓝 |
| Reservoir B (水) | 水蓝 |
| Deposit | 黑色 |
| Person (未搬运/被搬运) | 紫色/品红 |
| Fire A (化学) | 鞍棕色 |
| Fire B (非化学) | 橙红 |

如果 `save_frames=True`，每步保存 PNG 到 `render/{N}_agents/seed_{S}/scene_{N}/frame_{N}.png`。

---

## 六、动作系统 — LLM 输出 → 执行

### 6.1 语义相似度匹配 (`object_actions.py`)

由于 LLM 可能输出 `"use water on the great fire"` 而不是规范的 `"UseSupply(GreatFire, Water)"`，系统使用 **Sentence-BERT** 做语义匹配：

```python
model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
```

**流程：**
1. **预编译所有规范动作**：`all_actions_embeddings()` 枚举所有可能的规范动作字符串（如 `"NavigateTo(GreatFire_Region_1)"`、`"UseSupply(GreatFire, Sand)"` 等），计算并缓存它们的嵌入向量
2. **运行时匹配**：`get_closest_feasible_action()` 将 LLM 输出编码为向量，计算与所有规范动作的余弦相似度，选最高分
3. **数字修正**：如果 LLM 输出未指定区域编号但匹配的动作需要，默认编号 = 1

**`@hashable_with_cache`** 装饰器确保嵌入仅计算一次（缓存基于参数字典的哈希值）。

### 6.2 动作枚举规模

每个场景自动枚举数十到数百个规范动作：

| 类别 | 数量公式 |
|------|---------|
| `NavigateTo(o)` | 每个可达对象 |
| `Move(d)` | 5 方向 |
| `Carry(p)` | 每个人员 |
| `DropOff(d, p)` | 存放点×人员 |
| `UseSupply(f, s)` | 火灾×资源类型 |
| `GetSupply(d/r, s)` | 存放点×类型 + 每个水库 |

---

## 七、LLaMAR 基线循环

### 7.1 入口 (`baselines/llamar.py`)

```bash
python SAR/baselines/llamar.py --scene=1 --name='sar_test' --agents=2 --seed=0
```

**参数：**
| 参数 | 默认 | 说明 |
|------|------|------|
| `--scene` | 1 | 场景编号 (1-5) |
| `--agents` | 2 | 智能体数量 (2-6) |
| `--seed` | 42 | 随机种子 |
| `--name` | "default_location" | 结果目录名 |
| `--verbose` | False | 打印完整 LLM 输出 |
| `--tiny_verbose` | False | 仅打印最终指标 |

### 7.2 四模块循环

```text
  [初始 Planner 调用]  →  生成子任务计划
       ↓
  for step in range(timeout):
      ↓
  ┌── 1. update_plan() ───────────── 将 LLM 计划写入环境状态
  │   ↓
  │   2. ACTOR (LLM) ─────────────── 选择每个智能体的动作
  │   ↓
  │   3. action_mapping() ────────── 语义匹配 → 规范动作
  │   ↓
  │   4. env.step(actions) ───────── 执行动作，获得观测 + 成败
  │   ↓
  │   5. VERIFIER (LLM) ──────────── 判断哪些子任务已完成
  │   ↓
  │   6. env.closed_subtasks ← verifier 输出
  │   ↓
  │   7. PLANNER (LLM) ───────────── 根据新状态更新计划
  │   ↓
  │   8. checker metrics ─────────── coverage, transport_rate
  │   ↓
  │   9. 日志记录
  │   ↓
  └── 10. 如果全 "Done" → 提前退出
```

### 7.3 Prompt 模板 (`llamar_utils_multiagent.py`)

三个 LLM 模块各有专属 Prompt：

| 模块 | 角色 | 输入 | 输出 |
|------|------|------|------|
| **PLANNER** | "excellent planner" | 任务 + 各智能体观测 + 子任务状态 + 内存 | `{reason, plan: [...]}` |
| **ACTOR** | "excellent planner and robot controller" | 最丰富：任务 + 观测 + 状态 + 动作历史 + 失败历史 + 子任务 + 内存 | `{reason, subtask, memory, failure_reason, "<Name>'s action"}` |
| **VERIFIER** | "excellent planner" | 任务 + 观测 + 状态 + 动作历史 + 子任务 + 内存 | `{reason, "completed subtasks": [...]}` |

所有 LLM 调用使用 **`gpt-4-turbo`**，通过 `requests.post` 直接调用 OpenAI API（非 LangChain）。

**每步 3 次 LLM 调用**：Actor → Verifier → Planner（初始还有一次 Planner）。

### 7.4 重试机制

每个 LLM 调用包装在指数退避的 `while True` 循环中，捕获所有异常。解析采用三层正则回退：
1. ` ```json ... ``` `
2. ` ```python ... ``` `
3. 任何 ` ``` ... ``` ` 代码块
4. 原始输出文本

### 7.5 日志

`SARLogger` 写入 `results/{name}/`：
- `trajectory.csv` — 步数、动作、成功、覆盖、运输率、完成状态
- `memory.csv` — 步数、动作、原因、子任务、内存
- `render/` — 每帧 PNG（如启用）

---

## 八、关键设计模式与值得注意的地方

### 8.1 装饰器组合 vs 继承

系统使用**类包装装饰器**而非传统继承，每个装饰器返回一个包装类。这与 Python 的 `@functools.wraps` 不同，是真正的类变换。好处是对象可以任意组合能力（例如 `Flammable` 不可碰撞，`Person` 可碰撞）。

### 8.2 部分可观测性的实现

- 每个 POI 有 `radius`（视野半径），默认为 `3*sqrt(2)`（约 2 层 Moore 邻域）
- `Field.update_visibility()` 逐智能体检查可见性
- 火初始为 `impotent=True`，被观测后才激活蔓延
- 人员一旦被任何智能体发现 → 永久全局可见

### 8.3 多智能体协同的同步挑战

- **火灾蔓延**: 所有智能体都执行完动作 → 统一推进仿真时钟 → 火蔓延
- **人员搬运**: 需所有耦合智能体同步在存放点 `DropOff` 才能成功
- **`SAREnv.step()` 的 DropOff 修正**: 任一成功 → 全标记成功（解决多智能体时序冲突）

### 8.4 不是 Gymnasium

SAR 环境**不是**强化学习接口 — 没有 `observation_space`/`action_space`，没有 `reward`，没有 `terminated`/`truncated` 标志。它是为 LLM 规划循环量身定制的文本接口。

### 8.5 常见坑

| 问题 | 位置 | 说明 |
|------|------|------|
| 火区域命名 | `core.py` Fire | 火对象有 `_Region_N` 后缀，LLM 需要瞄准具体区域 |
| 类型匹配灭火 | `core.py` Flammable | A 型火需要沙，B 型需要水，类型错误无效 |
| 人员搬运最少人数 | `core.py` Person | 默认 ≥2 智能体才能搬动 |
| 库存限制 | `core.py` AbsAgent | 容量 = 3，携带人员时占满全部槽位 |
| 双花括号 | `base_env.py` | LangChain 兼容的 `{{` 转义（当前未使用 LangChain） |

---

## 九、如何扩展

### 添加新场景

1. 在 `Scenes/` 下创建 `scene_6.py`，遵循 `scene_1.py` 的模板，设置 `name`、`params` 和 `task_timeout`
2. 如果需新检查逻辑，在 `checker.py` 中扩展 `Checker`
3. 在 `get_scene_init.py` 中添加映射

### 修改火动力学

- 调整 `Flammable.L_TO_M`（默认 3）和 `M_TO_H`（默认 3）改变蔓延速度
- 调整 `Fire.STEP_EVERY`（默认 2）改变检查频率
- 修改 `procedural_generation()` 的 `shape` 参数添加新火形状

### 扩展动作空间

1. 在 `Controller.raw_step()` 中添加新动作分支
2. 在 `Controller.ALL_ACTIONS` 中注册
3. 更新 `Controller.restricted_actions`（如适用）
4. 在 `base_env.py` 的 `parse_action()` 中添加解析
5. 在 `object_actions.py` 的 `all_actions_embeddings()` 中添加枚举
6. 在 Prompt 模板中添加动作描述

### 调整奖励/成功标准

- 修改 `base_checker.py` 中的 `subtasks` 定义
- 修改 `Checker.__init__()` 或 `callback()` 中的条件判断
- `CheckSuccess()` 比较 `completed` vs `total`

---

## 十、文件依赖关系图

```text
llamar.py
  ├── llamar_utils_multiagent.py
  │     ├── env.py ───────────────────────┐
  │     │     ├── base_env.py             │
  │     │     │     └── misc.py           │
  │     │     ├── core.py                 │
  │     │     │     ├── misc.py           │
  │     │     │     └── base_env.py       │
  │     │     └── object_actions.py       │
  │     │           ├── misc.py           │
  │     │           └── sentence-transformers│
  │     └── sar_logging.py               │
  └── Scenes/
        ├── get_scene_init.py
        ├── scene_{1..5}.py
        ├── scene_initializer.py
        │     └── misc.py
        ├── checker.py
        │     └── base_checker.py
        └── render_scene.py
              └── utils.py
```

---

> 本指南基于 LLaMAR 项目代码生成，核心文件 `SAR/core.py` 共 ~2572 行，`SAR/env.py` 共 ~658 行。

---
日期: 2026-06-12
文档类型: 项目文档
文档概述: LLaMAR 代码框架文档 — 项目结构、架构设计、关键模式和使用指南
---

# LLaMAR — Code Framework Reference

## Project Overview

LLaMAR (Long-Horizon Planning for Multi-Agent Robots) is a VLM-based cognitive architecture for multi-agent planning in partially observable environments. Paper published at **NeurIPS 2024**. The framework implements a **Plan-Act-Correct-Verify** loop powered by GPT-4V/Turbo.

**Two experimental domains:**
- **MAP-THOR** (`AI2Thor/`) — household manipulation tasks in AI2-THOR 3D simulator (2 robots: Alice & Bob)
- **Search & Rescue** (`SAR/`) — grid-world firefighting + person rescue with custom engine (2–6 agents)

---

## System Documentation (`docs/system_docs/`)

| Document | Description |
|----------|-------------|
| [`docs/system_docs/框架.md`](docs/system_docs/框架.md) | System framework overview — A2A transport, Agent framework, SAR orchestration, data flow, config |
| [`docs/system_docs/data_flow.md`](docs/system_docs/data_flow.md) | Complete data flow — context_id / task_id / query lifecycle through A2A → Coordinator → Worker → Barrier |
| [`docs/system_docs/logging_map.md`](docs/system_docs/logging_map.md) | Logging system mapping — every record point, trigger, fields, output files |

## Architecture Overview

```
┌─────────────────────────────────────────────────┐
│                  LLaMAR Loop                      │
│                                                    │
│   Planner ──► Actor ──► Corrector ──► Verifier    │
│      ▲                                       │     │
│      └────────────── feedback ──────────────┘     │
└─────────────────────────────────────────────────┘
          │                    ▲
          ▼                    │
┌──────────────────┐   ┌──────────────────┐
│   AI2-THOR Env    │   │   SAR Grid Env    │
│ (AI2Thor/ dir)    │   │  (SAR/ dir)       │
│ - 3D indoor sim   │   │ - Custom grid     │
│ - 2-agent tasks   │   │ - 2-6 agent tasks │
│ - Object manip.   │   │ - Fire + Rescue   │
└──────────────────┘   └──────────────────┘
```

### The 4 Modules (Prompt-Engineered)

| Module | Role | Key Prompt File |
|--------|------|-----------------|
| **Planner** | Breaks task into subtask list | `AI2Thor/llm.py:PLANNER_PROMPT` |
| **Actor** | Chooses high-level action per agent from observations+memory | `AI2Thor/llm.py:ACTION_PROMPT` |
| **Corrector** | Identifies action failures, suggests fixes | Embedded in Actor/Verifier logic |
| **Verifier** | Confirms subtask completion, updates progress | `AI2Thor/llm.py:VERIFIER_PROMPT` |

---

## Directory Map

```
LLaMAR/
├── AI2Thor/                    # MAP-THOR (AI2-THOR 3D household tasks)
│   ├── base_env.py             # BaseEnv: coordinate parsing, action text generation, navigation
│   ├── env_new.py              # AI2ThorEnv: full AI2-THOR multi-agent environment
│   ├── smartLLM_env.py         # SmartLLM variant with LLM-cached observations
│   ├── explore.py              # CLIP/SentenceTransformer-based object search
│   ├── llm.py                  # ALL LLM prompts (Planner, Verifier, Actor) — ~110 lines
│   ├── object_actions.py       # Interactable object filtering for AI2-THOR
│   ├── failure_examples_reasons.py  # Failure case analysis utilities
│   └── test.py                 # Basic AI2-THOR test script
│
├── SAR/                        # Search & Rescue (custom grid-world)
│   ├── core.py                 # ★ MONOLITH (~2530 lines): entire SAR engine
│   │   ├── Coordinate          #   Grid coordinate system (default 3×3, extensible)
│   │   ├── GPS                 #   Global position tracker (static, tracks all objects)
│   │   ├── Flammable           #   Fire cell with intensity levels (NONE→LOW→MEDIUM→HIGH)
│   │   ├── Fire                #   Aggregate fire with spread mechanics
│   │   ├── Reservoir / Deposit #   Resource sources / sinks (infinite capacity by default)
│   │   ├── Person              #   Rescue target (needs ≥2 agents to carry)
│   │   ├── AbsAgent            #   Abstract agent with inventory (capacity=3)
│   │   ├── Field               #   World state: all objects + visibility tracking
│   │   ├── Controller          #   Action dispatch + observation generation
│   │   ├── Backend / GridEngine #   Low-level movement + collision
│   │   └── procedural_generation  # Scene generation from params
│   ├── Scenes/                 # Scene definitions (scene_1 through scene_5)
│   │   ├── base_checker.py     # Task completion checking
│   │   ├── checker.py          # Scene-specific checkers
│   │   ├── scene_initializer.py # BaseSceneInitializer: validates and preps scene params
│   │   ├── get_scene_init.py   # Scene factory (maps scene number → initializer)
│   │   ├── render_scene.py     # Scene visualization
│   │   └── scene_{1,2,3,4,5}.py  # Individual scene parameter definitions
│   ├── baselines/
│   │   ├── llamar.py           # LLaMAR baseline entry point for SAR
│   │   ├── llamar_utils_multiagent.py  # LLaMAR-specific utilities (LLM calls, parsing)
│   │   ├── sar_logging.py      # SAR experiment logging
│   │   └── multiagent_config.json  # Agent count configuration
│   ├── env.py                  # SAREnv: high-level env wrapper around Controller
│   ├── env_unittest.py         # Environment unit tests
│   ├── core_unittest.py        # Core engine unit tests
│   ├── misc.py                 # Utilities: Arg, join_conjunction, set_seed, etc.
│   ├── utils.py                # Additional SAR utilities
│   ├── print.py / meta_bash.py # Logging and shell helpers
│   ├── prod_transformer.py     # Production transformer (result formatting)
│   ├── analyze_results.py      # Result analysis
│   ├── object_actions.py       # Action space definitions for SAR
│   ├── llamar.sh / script.sh   # Batch run scripts
│   └── debug.py                # Debugging utilities
│
├── thortils/                   # AI2-THOR utility library (MIT licensed, adapted from zkytony)
│   ├── __init__.py             # Public API surface — imports all key functions
│   ├── controller.py           # launch_controller(): THOR lifecycle management
│   ├── navigation.py           # A* pathfinding, get_shortest_path_to_object()
│   ├── agent.py                # Agent pose/teleport/position utilities
│   ├── object.py               # Object detection, interaction, bbox utilities
│   ├── grid_map.py             # 2D grid map representation
│   ├── map3d.py                # 3D mapping (Mapper3D)
│   ├── interactions.py         # High-level interaction actions (Open, Close, Pickup, etc.)
│   ├── scene.py                # Scene loading and grid conversion
│   ├── constants.py            # Camera FOV, grid size, rotation angles, etc.
│   └── vision/                 # Vision utilities (projection, bboxes)
│
├── configs/                    # MAP-THOR task definitions (4 types, see below)
├── init_maker/                 # GUI tool for creating new AI2-THOR scene initializations
├── plots/                      # Paper figure generation (success_rate, coverage, transport)
├── results/                    # Experiment output logs (TSV format)
├── meta/                       # Meta-scripts: result compilation, plotting, shell helpers
├── vlms/                       # Open-source VLM test images
└── requirements.txt            # 95+ pinned dependencies
```

---

## MAP-THOR Task Types (`configs/`)

| Config | Type | Description | Example |
|--------|------|-------------|---------|
| `config_type1.json` | Explicit object, quantity, target | Everything specified | "Put the bread, lettuce and tomato in the fridge" |
| `config_type2.json` | Explicit object, target; ambiguous quantity | Quantity implicit ("all") | "Put all the apples in the fridge" |
| `config_type3.json` | Explicit target, implicit objects | Category-based objects | "Put all groceries in the fridge" |
| `config_type4.json` | Implicit target and objects | Fully ambiguous | "Clear the floor — put items in appropriate places" |

Each task JSON has: `task_name`, `task_description` (NL input), `task_id`, `task_type` (transport/manipulation), `task_timeout`, `task_floorplans`, `task_checklist`.

---

## SAR Engine Design (`SAR/core.py`)

### Class Hierarchy

```
Coordinate          — grid position (x,y,z), distance, bounds checking
GPS (static)        — global position → object_id mapping
Field               — world container: fires, reservoirs, deposits, agents, persons
Controller          — action dispatch, observation formatting, LLM interface
Backend             — engine abstraction layer
GridEngine          — grid-world movement + collision detection
```

### Decorator Pattern
Objects are composed via decorators rather than inheritance:
- `@with_id` — assigns unique UUID-based id
- `@with_position(mutable=True/False)` — adds position + visibility radius
- `@named` — adds human-readable name
- `@collidable(is_collidable=True/False)` — controls navigation blocking

### Action Space (SAR)
| Category | Actions |
|----------|---------|
| Movement | `NavigateTo(obj_id)`, `Move(direction)` — Up/Down/Left/Right/Center/Diagonals |
| Carry/Drop | `Carry(person_id)`, `DropOff(person_id, deposit_id)` |
| Supply | `GetSupply(source_id, type)`, `StoreSupply(deposit_id)`, `UseSupply(fire_id, type)`, `ClearInventory` |
| Meta | `NoOp` |

### Fire Mechanics
- Intensity: `NONE → LOW → MEDIUM → HIGH` (clock-driven progression)
- Spread: occurs when intensity ≥ MEDIUM, spreads to neighboring Flammable cells
- Extinguishing: requires correct resource type (A→sand/water, B→water/sand depending on config)
- Fires are initially `impotent` (don't spread until discovered by an agent)

### Person Rescue
- `Person.load` = 2 + extra_load (minimum agents to carry)
- Agents couple/uncouple via `Person.pick()` / `Person.drop()`
- Drop succeeds only when ALL coupled agents are at deposit location
- Once deposited, person becomes invisible (`deposited=True`)

---

## Key Conventions & Gotchas

### Code Patterns
- **`@bug` comments**: Known issues or workarounds — read before modifying
- **`@tt` comments**: Temporary/test notes
- **`@here` comments**: Attention points for future work
- **`@multiagent` comments**: Multi-agent specific logic markers
- **Double-brace escaping**: `convert_dict_to_string_with_double_braces()` needed for LangChain formatting (replaces `{` → `{{`)

### Navigation (AI2Thor/)
- `get_shortest_path_to_object()` in `thortils/navigation.py` returns `(poses, actions)` for A* pathfinding
- Navigation actions include `AlignOrientation(pitch, yaw, z)` wrappers for camera alignment
- Agent rotation is snapped to nearest cardinal direction (0, 90, 180, 270)

### Navigation (SAR/)
- `GridEngine.navigation()` does simple collision-free placement within radius — no A*
- `Controller.TO_DELTA_MAP` maps direction names to (dx, dy) tuples
- Objects with `collidable=True` block navigation

### LLM Interaction
- All prompts live in `AI2Thor/llm.py` as module-level string constants
- Output format is strictly Python dict parsed via `ast.literal_eval()` or regex
- OpenAI API key loaded from `openai_key.json` at project root: `{"my_openai_api_key": "..."}`
- Model changed from deprecated `gpt-4-vision-preview` → `gpt-4-turbo` (commit `d2b74ca`)

### Known Issues
- `requirements.txt` includes OS-specific packages (e.g., `appnope` for macOS)
- `requirements.txt` has a commented-out `open3d` line — install separately if needed
- README references `AI2Thor/baselines/llamar/llamar.py` but actual file is at `SAR/baselines/llamar.py`
- README references `AI2Thor/baselines/CoT/CoT.py` which doesn't exist in the repo

---

## Common Commands

### Run MAP-THOR experiment
```bash
# Single task on single floorplan
python AI2Thor/baselines/llamar/llamar.py --task=0 --floorplan=0

# Batch (edit .sh file to select tasks/floorplans)
bash AI2Thor/baselines/llamar/llamar.sh
```

### Run SAR experiment
```bash
python SAR/baselines/llamar.py --scene=1 --name='llamar_SAR' --agents=2 --seed=0
```

### Run unit tests
```bash
python SAR/env_unittest.py
python SAR/core_unittest.py
```

### Create new AI2-THOR scene
```bash
python init_maker/game.py
```

---

## Dependencies Quick Reference

| Package | Version | Purpose |
|---------|---------|---------|
| `ai2thor` | 5.0.0 | 3D indoor simulator |
| `openai` | 1.11.1 | GPT-4V/Turbo API |
| `torch` | 2.2.0 | Deep learning backend |
| `transformers` | 4.38.0 | HuggingFace models |
| `sentence-transformers` | 2.3.1 | Semantic embeddings |
| `opencv-python` | 4.9.0.80 | Image processing |
| `numpy` | 1.26.4 | Numerical computing |
| `matplotlib` | 3.8.2 | Plotting |

**Installation**: `pip install -r requirements.txt` (then install open3d separately if needed)

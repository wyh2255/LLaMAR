# AI2Thor API & Architecture Research Report

## Overview

AI2Thor (Allen Institute for Artificial Intelligence - THOR) is a **Unity-based 3D embodied AI simulation environment** developed by the PRIOR team at AI2. It features near photo-realistic indoor scenes with interactive objects, supporting navigation and object manipulation tasks for embodied AI agents.

**Version**: 5.0.0 (pinned in this project's `requirements.txt`)
**Paper**: [AI2-THOR: An Interactive 3D Environment for Visual AI](https://arxiv.org/abs/1712.05474)
**Website**: https://ai2thor.allenai.org
**GitHub**: https://github.com/allenai/ai2thor

---

## 1. Installation & Setup

### Pip installation
```bash
pip install ai2thor
```

### System Requirements
| Component | Requirement |
|-----------|-------------|
| OS | macOS 10.9+, Ubuntu 14.04+ |
| Graphics | DX9 (shader model 3.0) or DX11 with feature level 9.3 |
| CPU | SSE2 instruction set support |
| Python | 3.5+ |
| Linux | X server with GLX module enabled |

### Unity Binary Management
- On first import, `pip install ai2thor` automatically downloads the appropriate Unity build from GitHub releases for your platform
- The downloaded build is cached locally (under `~/.ai2thor/`)
- Custom builds can be specified: `Controller(local_executable_path="/path/to/thor-build")`
- The `ai2thor.build` module handles build discovery, download, and caching

### This Project
- `ai2thor==5.0.0` is in `requirements.txt` but **not currently installed** in the environment
- Several WSL-specific considerations may apply (X server via VcXsrv/Xming needed for rendering)

---

## 2. Controller API

### Creating a Controller
```python
from ai2thor.controller import Controller

# Simplest usage — auto-downloads Unity binary and starts it
controller = Controller(scene="FloorPlan1")

# With custom settings
controller = Controller(
    width=800,           # Render resolution
    height=800,
    scene="FloorPlan1",  # Initial scene
    gridSize=0.25,       # Grid snapping size
    headless=False,      # Set True for servers without display
    platform=CloudRendering,  # Use cloud rendering engine
)
```

### Key Controller Methods

- **`controller.step(action)`** — Core method. Sends an action to Unity, executes it, returns an `Event` object. Accepts a string action name or a dict with full parameters.
- **`controller.reset(scene)`** — Resets to a new scene (or same scene with fresh state).
- **`controller.stop()`** — Stops the Unity process.
- **`controller.last_event`** — The most recently returned Event.

### Step Return Value
```python
event = controller.step(action="MoveAhead")
# event is an Event or MultiAgentEvent object containing:
# event.metadata         — dict with all state information
# event.frame            — raw RGB numpy array
# event.cv2img           — OpenCV-compatible RGB image
# event.depth_frame      — depth frame (if enabled)
# event.instance_detections2D  — instance segmentation data
```

---

## 3. Event & Observation Model

### Event Class
Every `controller.step()` returns an `Event` (single agent) or `MultiAgentEvent` (multi-agent):

```python
class Event:
    metadata          # Dict: agent state, object states, action result
    frame             # Raw RGB frame (numpy array)
    cv2img            # OpenCV-formatted RGB image
    depth_frame       # Depth frame
    normals_frame     # Surface normals
    flow_frame        # Optical flow
    instance_detections2D  # LazyInstanceSegmentationMasks
    class_masks       # LazyClassSegmentationMasks
    instance_segmentation_frame
    semantic_segmentation_frame
    third_party_camera_frames  # From AddThirdPartyCamera action
    events = [self]   # Always a list (self for single agent, all agents for multi)
```

### MultiAgentEvent (returned when using multiple agents)
```python
class MultiAgentEvent:
    events = [event0, event1, ...]  # One Event per agent
    metadata = events[active_agent_id].metadata  # Shorthand for active agent
    cv2img  = events[active_agent_id].cv2img     # Shorthand
    
    # Access per-agent data:
    event.events[agent_id].metadata["agent"]["position"]
    event.events[agent_id].cv2img
    event.events[agent_id].instance_detections2D
```

### Metadata Structure
The `event.metadata` dict contains:

| Key | Description |
|-----|-------------|
| `agent` | Agent state: `position {x,y,z}`, `rotation {x,y,z}`, `cameraHorizon`, `isStanding` |
| `agents` | List of all agents' states (multi-agent) |
| `objects` | List of all objects with their properties (position, state, type, etc.) |
| `inventoryObjects` | Objects held by the agent |
| `lastAction` | The action string that was just executed |
| `lastActionSuccess` | Boolean indicating if the action succeeded |
| `errorMessage` | Error description on failure |
| `errorCode` | Categorized error code |
| `actionReturn` | Return value from informational actions |
| `sceneName` | Current scene name |
| `screenWidth`/`screenHeight` | Render resolution |
| `colors` | Color mapping used for segmentation |

### Sensor Outputs (per agent in multi-agent mode)
```python
# RGB image
event.events[agent_id].frame         # (H, W, 3) uint8 numpy array
event.events[agent_id].cv2img        # Same, OpenCV BGR format

# Instance segmentation
event.events[agent_id].instance_detections2D  # LazyInstanceSegmentationMasks
detections = event.events[agent_id].instance_detections2D
list(detections.instance_masks.keys())  # ['Cabinet|...', 'Apple|...', ...]
detections['Apple|-00.47|+01.15|+00.48']  # Binary mask numpy array

# Agent metadata
event.events[agent_id].metadata["agent"]["position"]  # {x: float, y: float, z: float}
event.events[agent_id].metadata["agent"]["rotation"]  # {x: float, y: float, z: float}
event.events[agent_id].metadata["inventoryObjects"]   # List of held objects
```

---

## 4. Action API

### Action Structure
Actions are sent as a dict with `action` key plus parameters:
```python
controller.step(dict(action="MoveAhead"))
controller.step(dict(action="PickupObject", objectId="Apple|-00.47|+01.15|+00.48"))
controller.step(dict(action="RotateRight"))
controller.step(dict(action="Teleport", position=dict(x=1.5, y=0.9, z=-1.5), rotation=dict(x=0, y=270, z=0)))
```

### Complete Action Catalog

#### Navigation Actions
| Action | Parameters | Description |
|--------|-----------|-------------|
| `MoveAhead` | — | Move forward one grid cell |
| `MoveBack` | — | Move backward one grid cell |
| `MoveLeft` | — | Move left one grid cell |
| `MoveRight` | — | Move right one grid cell |
| `RotateRight` | — | Rotate 90° right |
| `RotateLeft` | — | Rotate 90° left |
| `LookUp` | `degrees` | Tilt camera up |
| `LookDown` | `degrees` | Tilt camera down |
| `Teleport` | `position`, `rotation`, `horizon`, `standing` | Teleport to absolute position |
| `TeleportFull` | `x`, `y`, `z`, `rotation`, `horizon`, `standing` | Full teleport with all params |
| `GetReachablePositions` | — | Returns all navigable positions |

#### Object Interaction Actions
| Action | Parameters | Description |
|--------|-----------|-------------|
| `PickupObject` | `objectId` | Pick up an object |
| `PutObject` | `objectId`, `receptacleObjectId` | Place held object on receptacle |
| `OpenObject` | `objectId` | Open a receptacle (cabinet, fridge, etc.) |
| `CloseObject` | `objectId` | Close a receptacle |
| `ToggleObjectOn` | `objectId` | Turn on an object (lamp, stove, etc.) |
| `ToggleObjectOff` | `objectId` | Turn off an object |
| `SliceObject` | `objectId` | Slice a sliceable object (bread, tomato, etc.) |
| `CleanObject` | `objectId` | Clean a dirty object |
| `DropHandObject` | — | Drop held object |
| `ThrowObject` | `moveMagnitude` | Throw held object |
| `PushObject` | `objectId`, `moveMagnitude`, `pushAngle` | Push an object |
| `PullObject` | `objectId`, `moveMagnitude` | Pull an object |
| `DirectionalPush` | `objectId`, `moveMagnitude`, `pushAngle` | Push in specific direction |

#### Held Object Manipulation
| Action | Parameters | Description |
|--------|-----------|-------------|
| `MoveHeldObjectAhead` | `moveMagnitude` | Move held object forward |
| `MoveHeldObjectBack` | `moveMagnitude` | Move held object backward |
| `MoveHeldObjectLeft` | `moveMagnitude` | Move held object left |
| `MoveHeldObjectRight` | `moveMagnitude` | Move held object right |
| `MoveHeldObjectUp` | `moveMagnitude` | Move held object up |
| `MoveHeldObjectDown` | `moveMagnitude` | Move held object down |
| `MoveHeldObject` | `ahead`, `right`, `up` | Move held object by vector |
| `RotateHeldObject` | `pitch`, `yaw`, `roll` | Rotate held object |

#### Initialization / Special Actions
| Action | Parameters | Description |
|--------|-----------|-------------|
| `Initialize` | `gridSize`, `agentCount`, `renderObjectImage`, `renderClassImage`, `visibilityDistance` | Initialize scene with settings |
| `InitialRandomSpawn` | `randomSeed` | Randomize object positions |
| `Pass` | — | No-op (advance one frame) |
| `Done` | — | Signal task completion |
| `Reset` | `sceneName` | Reset to scene (used internally by `controller.reset()`) |
| `ChangeResolution` | `x`, `y` | Change render resolution |
| `AddThirdPartyCamera` | `position`, `rotation` | Add an extra camera |
| `ToggleMapView` | — | Toggle top-down map view |
| `SpecificToggleSpecificState` | `StateChange`, `objectId` | Set specific object state (Break, Dirty, Fill, etc.) |
| `PlaceObjectAtPoint` | `objectId`, `position` | Place object at specific position |
| `RandomizeMaterials` | — | Randomize object materials/textures |
| `RandomizeLighting` | — | Randomize scene lighting |

### Multi-Agent Action Routing
In multi-agent mode, specify which agent should act:
```python
controller.step(dict(action="MoveAhead", agentId=0))
controller.step(dict(action="MoveAhead", agentId=1))
```
When `agentCount=1`, the `agentId` parameter can be omitted (defaults to 0).

---

## 5. Multi-Agent Support

AI2Thor natively supports **multiple agents** in the same scene.

### Setting Up Multi-Agent
```python
controller = Controller(scene="FloorPlan1")
# Initialize with agentCount > 1
event = controller.step(dict(
    action="Initialize",
    gridSize=0.25,
    agentCount=2,          # Number of agents
    renderObjectImage=True,
    visibilityDistance=1.5
))

# event is now a MultiAgentEvent with event.events[0] and event.events[1]
print(event.events[0].metadata["agentId"])  # 0
print(event.events[1].metadata["agentId"])  # 1
```

### Multi-Agent Observations
```python
event.events[0].metadata["agent"]["position"]  # Agent 0's position
event.events[0].cv2img                         # Agent 0's camera view
event.events[0].instance_detections2D          # Agent 0's detections
event.events[1].metadata["agent"]["position"]  # Agent 1's position
```

### Multi-Agent Action Execution
Each step executes one action for exactly **one agent**. To coordinate multi-agent actions:
```python
# Agent 0 moves
event = controller.step(dict(action="MoveAhead", agentId=0))
# Agent 1 picks up an object
event = controller.step(dict(action="PickupObject", objectId="Apple|-...", agentId=1))
```

**Important**: AI2Thor does NOT natively support parallel multi-agent steps. The LLaMAR project handles this by executing actions sequentially in a loop within their `env.step()` method, but all agents' actions for the "same timestep" are serialized. This means the first agent's action can affect the second agent's world state (e.g., agent 0 moving could block agent 1's path).

### Agent Names in this Project
```python
AGENT_NAMES = ["Alice", "Bob", "Charlie", "David", "Emma"]  # env_new.py
```

---

## 6. Scenes & Environment Types

### Scene Types (iTHOR)
AI2Thor provides 120 iTHOR scenes organized by room type:

| Type | Scene IDs | Count |
|------|-----------|-------|
| Kitchens | `FloorPlan1` - `FloorPlan30` | 30 |
| Living Rooms | `FloorPlan201` - `FloorPlan230` | 30 |
| Bedrooms | `FloorPlan301` - `FloorPlan330` | 30 |
| Bathrooms | `FloorPlan401` - `FloorPlan430` | 30 |

### Scene Initialization
```python
# Direct scene specification in Controller
controller = Controller(scene="FloorPlan1")

# Or via Initialize action (recommended for multi-agent)
event = controller.step(dict(action="Initialize", agentCount=2, ...))

# Domain randomization
event = controller.step(dict(action="InitialRandomSpawn", randomSeed=42))
event = controller.step(dict(action="RandomizeMaterials"))
event = controller.step(dict(action="RandomizeLighting"))
```

### Object Naming Convention
Objects have unique IDs with embedded coordinates:
```
ObjectType|+XX.XX|+YY.YY|+ZZ.ZZ
```
Examples:
- `Apple|-00.47|+01.15|+00.48` → Apple at position (-0.47, 1.15, 0.48)
- `Cabinet|-01.85|+02.02|+00.38` → Cabinet at (-1.85, 2.02, 0.38)

### Object Metadata (per object in event.metadata["objects"])
```python
{
    "objectId": "Apple|-00.47|+01.15|+00.48",
    "objectType": "Apple",
    "position": {"x": -0.47, "y": 1.15, "z": 0.48},
    "rotation": {"x": 0, "y": 0, "z": 0},
    "visible": True,
    "distance": 0.5,              # Distance from agent
    "pickupable": True,
    "moveable": True,
    "openable": False,
    "toggleable": False,
    "sliceable": True,
    "canFillWithLiquid": False,
    "canBeUsedUp": False,
    "isBroken": False,
    "isDirty": False,
    "isFilledWithLiquid": False,
    "isUsedUp": False,
    "isOn": False,
    "salientMaterials": ["Vegetable"],  # Material properties
    "mass": 0.1,                  # Physical mass
    "temperature": "RoomTemp",    # Temperature state
    "receptacle": False,          # Can other objects be placed on it?
    "receptacleObjectIds": [],    # Objects currently in this receptacle
}
```

---

## 7. Step-Based Simulation Loop — How This Project Uses It

### Standard AI2Thor Loop
```python
controller = Controller(scene="FloorPlan1")
# Initialize
event = controller.step(dict(action="Initialize", agentCount=2, ...))

# Simulation loop
for step in range(max_steps):
    # Each step sends one action for one agent
    event = controller.step(dict(action="MoveAhead", agentId=0))
    event = controller.step(dict(action="PickupObject", objectId=obj_id, agentId=1))
    # Check metadata after each step
    if not event.events[0].metadata["lastActionSuccess"]:
        print("Agent 0's action failed")
```

### How env_new.py (AI2ThorEnv) Wraps the Loop
```python
# Step function executes all agents' actions sequentially
def step(self, actions: List[str]):
    for agent_id in range(self.num_agents):
        if "NavigateTo" in actions[agent_id]:
            # Navigation uses thortils to generate multiple sub-actions
            act_success, error_type = self.navigation_step(actions[agent_id], agent_id)
        elif actions[agent_id] == "Done" or "Idle":
            act_success = True
        else:
            # Parse action string like "PickupObject(Apple_1)" → action dict
            action_dict = self.parse_action(actions[agent_id], agent_id)
            self.event = self.controller.step(action_dict)
            act_success = self.event.events[agent_id].metadata["lastActionSuccess"]
    return obs_string, act_successes
```

### Important: Sequential Agent Execution
The project's `step()` loops over agents sequentially:
1. Agent 0's action is executed → `controller.step()` → event updated
2. Agent 1's action is executed → `controller.step()` → event updated
3. Both actions are reported as happening "at the same time" in the LLM prompt

This is a **simulation-level abstraction** — AI2Thor itself does not support parallel multi-agent steps.

---

## 8. Task/Scene Definition System in This Project

### Task Structure (`AI2Thor/Tasks/`)

Each task is a directory with per-scene initializers:
```
Tasks/
├── 1_put_plate_mug_bowl_fridge/
│   ├── FloorPlan1.py       # SceneInitializer class with preinit()
│   ├── FloorPlan2.py
│   └── ...
├── 2_put_all_vases_countertop/
│   └── ...
└── task_mapper.py          # Maps task descriptions to task IDs
    get_scene_init.py       # Maps (task, scene) to SceneInitializer class
```

### SceneInitializer Pattern
```python
# Tasks/1_put_plate_mug_bowl_fridge/FloorPlan1.py
class SceneInitializer:
    def preinit(self, event, controller):
        # Place objects at specific positions for the task
        event = controller.step(action='PlaceObjectAtPoint',
            objectId='Pot|-01.22|+00.90|-02.36',
            position={'x': 0, 'y': 0, 'z': 0})
        event = controller.step(action='PlaceObjectAtPoint',
            objectId='Bowl|+00.27|+01.10|-00.75',
            position={'x': -1.249, 'y': 0.9009, 'z': -2.356})
        return event
```

### Checker Pattern (Task Completion Check)
Each task has a `Checker` class (referenced in scene initializer) that tracks:
- `subtasks` — List of atomic subtasks required for completion
- `subtasks_completed` — Tracked via `perform_metric_check(action, success, inventory)`
- `check_success()` — Returns True if all subtasks completed
- `get_coverage()` / `get_transport_rate()` — Progress metrics

---

## 9. Navigation in This Project

### NavigateTo Action
The project implements a custom `NavigateTo` action that decomposes into multiple AI2Thor primitive actions:

```python
# User/LLM says: "NavigateTo(Apple_1)"
# Parsed to: 
actions = get_shortest_path_to_object(controller, other_agents, object_id, cur_pos, cur_rot)
# Returns a sequence like: ['MoveAhead', 'RotateLeft', 'MoveAhead', 'MoveAhead', 'AlignOrientation(...)']
```

The `thortils` library provides:
- `get_shortest_path_to_object()` — Plans navigation to an object avoiding other agents
- Handles obstacle avoidance, rotation planning, camera horizon adjustment

### Parsing System (base_env.py)
The `parse_action()` method converts LLM-friendly action strings to AI2Thor action dicts:

| LLM Output | Parsed Action Dict |
|-----------|-------------------|
| `Move(Ahead)` | `{action: 'MoveAhead', agentId: N}` |
| `Rotate(Left)` | `{action: 'RotateLeft', agentId: N}` |
| `PickupObject(Apple_1)` | `{action: 'PickupObject', objectId: 'Apple|...', agentId: N}` |
| `PutObject(CounterTop_1)` | `{action: 'PutObject', objectId: 'CounterTop|...', agentId: N}` |
| `LookUp(30)` | `{action: 'LookUp', degrees: 30, agentId: N}` |
| `AlignOrientation(pitch,yaw,z)` | `{action: 'TeleportFull', rotation: ..., position: ...}` |
| `Explore()` | `{action: 'Explore', agentId: N}` (handled specially) |

---

## 10. Key Differences from Current SAR Environment

| Dimension | SAR (Grid-World) | AI2Thor (3D Simulator) |
|-----------|-----------------|----------------------|
| **Environment** | Custom 30×30 discrete grid, pure Python | Unity 3D continuous space, GPU-required |
| **Dependencies** | Pure Python (no external engine) | Python + Unity binary (downloaded separately) |
| **Observations** | Text description (local + global grid view) | RGB image + instance/semantic segmentation + metadata |
| **Action Space** | 11 actions (NavigateTo, Move, Carry, UseSupply, etc.) | 40+ actions (Move, Rotate, Pickup, Put, Open, Close, etc.) |
| **Position System** | Discrete (x, y) grid coordinates, integer-based | Continuous (x, y, z) 3D coordinates, float-based |
| **Multi-Agent** | Natively supports 2-6 agents, parallel step | Supports N agents but **sequential step** |
| **Objects** | Abstract POIs (fires, persons, reservoirs, deposits) | 2600+ realistic 3D household objects across 100+ types |
| **State Changes** | Fire spread, person carry, resource depletion | Object temperature, cleanliness, broken state, liquid fill, slicing |
| **Object IDs** | Human-readable names (GreatFire, LostPersonTimmy) | `ObjectType|+X.XX|+Y.YY|+Z.ZZ` format |
| **Task Checking** | `Checker` tracks subtasks | No built-in task system — custom per-project |
| **Rendering** | Matplotlib grid map visualization | Unity 3D rendering (RGB, depth, normals, segmentation) |
| **Physics** | No physics | Unity physics (collision, gravity, object mass) |
| **Mounting** | Plug-and-play | Requires X server on Linux (WSL needs VcXsrv/Xming) |

### Key Integration Challenges

1. **3D → Text Translation**: AI2Thor returns images + segmentation. The existing project converts these to text descriptions of visible objects (using `instance_detections2D`). This is a richer but more complex observation space.

2. **Sequential Multi-Agent**: AI2Thor serializes multi-agent actions. The current project acknowledges this by wrapping it in a loop but it means agent 1 sees agent 0's updated state.

3. **Unity Dependency**: AI2Thor requires a running Unity process with GPU access. In WSL, this means configuring an X server (VcXsrv) or using headless mode with software rendering.

4. **Object ID Complexity**: AI2Thor's compound object IDs (`Apple|-00.47|+01.15|+00.48`) need conversion to/from human-readable format (`Apple_1`). The `BaseEnv.parse_object()` and `object_dict` system handles this mapping.

5. **Navigation is Primitive**: AI2Thor only provides `MoveAhead/Back/Left/Right` and `RotateLeft/Right`. The project uses `thortils` for path planning and decomposes navigation into a sequence of these primitives (unlike SAR's Teleport-based navigation).

6. **No Built-in Task System**: Unlike SAR's integrated checker system, AI2Thor tasks must be defined entirely by the project (via `SceneInitializer` + `Checker` classes).

---

## 11. Existing Repository Code Analysis

### Files Already in the Repository

| File | Purpose |
|------|---------|
| `AI2Thor/test.py` | Minimal example — creates controller, lists scenes, basic actions, shows multi-agent event access |
| `AI2Thor/base_env.py` | `BaseEnv` class — object ID parsing, action parsing, navigation methods, observation generation |
| `AI2Thor/env_new.py` | `AI2ThorEnv` class — primary environment wrapper, multi-agent step loop, LLM input formatting |
| `AI2Thor/smartLLM_env.py` | `SmartLLMEnv` class — baseline environment for SmartLLM paper |
| `AI2Thor/explore.py` | `ExploreEnv` — exploration strategy using CLIP/Sentence-BERT for optimal orientation |
| `AI2Thor/Tasks/` | Scene initializers and task definitions (put items in fridge, turn on knobs, etc.) |
| `AI2Thor/utils/scene_config.py` | Scene configuration helper — sets up custom initial object states |
| `AI2Thor/utils/replay_save_frames.py` | Replay and save frame utilities |
| `AI2Thor/baselines/` | Multiple baselines (SmartLLM, CoLA, ReAct, CoT, LLaMAR) each with their own env wrappers |
| `AI2Thor/object_actions.py` | (Referenced via import) — action matching, interaction detection |

### Notable Design Patterns
- **Action abstraction layer**: LLM → `NavigateTo(Apple_1)` → `parse_action()` → AI2Thor action dict → `controller.step()`
- **Object ID mapping**: `object_dict` maps human-readable `Apple_1` ↔ AI2Thor `Apple|-00.47|+01.15|+00.48`
- **Sequential multi-agent**: `step(actions)` loops over `agent_id` and calls `controller.step()` per agent
- **Navigation decomposition**: `NavigateTo(object)` → thortils path planning → sequence of Move/Rotate/Look actions

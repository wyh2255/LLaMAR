from dataclasses import dataclass, field


@dataclass
class RunMeta:
    run_id: str
    scene: int
    agent_count: int
    model: str
    seed: int
    state_mode: str
    success_criteria: str
    max_steps: int


@dataclass
class RunMetrics:
    coverage: float
    transport_rate: float
    steps: int
    finished: bool
    elapsed_seconds: float
    end_reason: str


@dataclass
class AgentTokenSummary:
    agent: str
    prompt: int
    completion: int
    total: int
    cache_hit: int
    cache_miss: int


@dataclass
class StepRecord:
    step: int
    actions: dict[str, str] = field(default_factory=dict)
    successes: dict[str, bool] = field(default_factory=dict)
    positions: dict[str, tuple] = field(default_factory=dict)
    inventories: dict[str, dict] = field(default_factory=dict)
    observations: dict[str, str] = field(default_factory=dict)
    coverage: float = 0.0
    transport_rate: float = 0.0
    timeout_agents: list[str] = field(default_factory=list)
    completed_subtasks_delta: int = 0


@dataclass
class TokenRecord:
    step: int
    agent: str
    prompt: int
    completion: int
    total: int
    cache_hit: int
    cache_miss: int


@dataclass
class Subtask:
    subtask_id: str
    step: int
    status: str
    assigned_to: str
    text: str


@dataclass
class RouterEvent:
    step: int
    subtask: str
    assigned_to: str
    event_type: str


@dataclass
class CoordinatorEvent:
    step: int
    event_type: str
    agent: str
    payload: dict


@dataclass
class SemanticObject:
    object_type: str
    name: str
    observations: list[dict] = field(default_factory=list)
    conflict: bool = False


@dataclass
class LLMTraceEvent:
    task_id: str
    agent: str
    ts: str
    event: str
    content: str = ""
    tool_calls: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    result: str = ""
    error: str = ""


@dataclass
class ReportData:
    meta: RunMeta | None = None
    metrics: RunMetrics | None = None
    summary: dict = field(default_factory=dict)
    agent_tokens: list[AgentTokenSummary] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    tokens: list[TokenRecord] = field(default_factory=list)
    subtasks: list[Subtask] = field(default_factory=list)
    router_events: list[RouterEvent] = field(default_factory=list)
    coordinator_events: list[CoordinatorEvent] = field(default_factory=list)
    semantic_objects: list[SemanticObject] = field(default_factory=list)
    llm_traces: dict[tuple[str, str], list[LLMTraceEvent]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

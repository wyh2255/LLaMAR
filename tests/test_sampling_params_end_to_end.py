"""T1 / E-1 regression: sampling params must reach the HTTP request body.

Why this file exists at all, and why the tests look redundant:

`temperature=0.7` was dead code for the whole project history. Three
`self._temperature` attributes were assigned and never read, `AgentBuildOptions`
had no field for it, and the `params` dicts sent to both providers contained no
sampling keys. Every individual hop looked correct in isolation -- the *chain*
was severed in six places. A per-layer unit test would have passed at every
layer while the feature did not work.

So the load-bearing test here is the end-to-end one: it starts above the LLM
client and asserts against the dict handed to the provider SDK. The per-layer
tests exist to localise a future break, not to establish correctness.

Also pinned, because each was a real failure mode rather than a hypothetical:

  * `None` must inject *nothing* -- distinct from injecting 0.0. Explicit 0.0
    is deterministic sampling; absent means "gateway decides".
  * `seed` must be filtered out for Anthropic, whose Messages API returns 400.
  * `llm_seed` must stay distinct from `seed` (the scene/env seed). Conflating
    them would silently make the variance baseline uninterpretable.
  * Defaults must stay 0.7 / 0.3 -- this task was plumbing only. A changed
    default would alter behaviour in the same commit as the plumbing.
"""

from __future__ import annotations

import inspect
from dataclasses import fields

import pytest

from Agent.router_agent.build import RouterBuildOptions
from Agent.worker_agent.build import AgentBuildOptions
from Agent.worker_agent.llm import LLMClient
from Agent.worker_agent.retry import RetryConfig
from Agent.worker_agent.schema import LLMProvider, Message, SamplingParams

DUMMY_KEY = "sk-test-dummy-not-a-real-key"


# ---------------------------------------------------------------------------
# SamplingParams semantics
# ---------------------------------------------------------------------------


class TestSamplingParams:
    def test_unset_fields_inject_nothing(self):
        """Absent != 0.0. Absent lets the gateway decide; 0.0 is a request."""
        assert SamplingParams().as_request_fields(LLMProvider.OPENAI) == {}

    def test_explicit_zero_is_not_folded_into_unset(self):
        got = SamplingParams(temperature=0.0).as_request_fields(LLMProvider.OPENAI)
        assert got == {"temperature": 0.0}

    def test_seed_reaches_openai(self):
        got = SamplingParams(temperature=0.5, seed=7).as_request_fields(LLMProvider.OPENAI)
        assert got == {"temperature": 0.5, "seed": 7}

    def test_seed_is_filtered_for_anthropic(self):
        """The Anthropic Messages API rejects `seed` with a 400."""
        got = SamplingParams(temperature=0.5, seed=7).as_request_fields(
            LLMProvider.ANTHROPIC
        )
        assert got == {"temperature": 0.5}

    def test_top_p_is_carried(self):
        assert SamplingParams(top_p=0.9).as_request_fields(LLMProvider.OPENAI) == {
            "top_p": 0.9
        }

    def test_is_frozen(self):
        """Sampling must be constant within a run: a mid-run change would be
        invisible in the variance baseline."""
        with pytest.raises(Exception):
            SamplingParams().temperature = 0.5  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The load-bearing test: does the value reach the request body?
# ---------------------------------------------------------------------------


def _capture_request_body(provider: LLMProvider, sampling: SamplingParams) -> dict:
    """Build a client, intercept at the provider SDK boundary, return the body.

    Patching at the SDK call rather than at `_make_api_request` is deliberate:
    the `params` dict is constructed *inside* that method, so patching the
    method itself would skip the code under test.
    """
    import asyncio

    client = LLMClient(
        api_key=DUMMY_KEY,
        provider=provider,
        api_base="https://example.invalid/v1",
        model="test-model",
        retry_config=RetryConfig(enabled=False),
        sampling=sampling,
    )
    captured: dict = {}

    async def sink(**params):
        captured.update(params)
        raise RuntimeError("captured -- no network call is made")

    if provider is LLMProvider.OPENAI:
        client._client.client.chat.completions.create = sink
    else:
        client._client.client.messages.create = sink

    with pytest.raises(Exception):
        asyncio.run(client.generate([Message(role="user", content="hi")]))

    return {k: v for k, v in captured.items() if k in ("temperature", "top_p", "seed")}


class TestReachesRequestBody:
    def test_openai_receives_temperature_and_seed(self):
        body = _capture_request_body(
            LLMProvider.OPENAI, SamplingParams(temperature=0.123, seed=456)
        )
        assert body == {"temperature": 0.123, "seed": 456}

    def test_anthropic_receives_temperature_without_seed(self):
        body = _capture_request_body(
            LLMProvider.ANTHROPIC, SamplingParams(temperature=0.123, seed=456)
        )
        assert body == {"temperature": 0.123}

    def test_unset_sampling_sends_no_sampling_keys(self):
        """The pre-fix behaviour, now only reachable by explicitly asking."""
        assert _capture_request_body(LLMProvider.OPENAI, SamplingParams()) == {}

    def test_explicit_zero_temperature_survives_the_whole_chain(self):
        body = _capture_request_body(LLMProvider.OPENAI, SamplingParams(temperature=0.0))
        assert body == {"temperature": 0.0}


# ---------------------------------------------------------------------------
# Per-layer: localise a future break. Both stacks, because they are duplicated
# and an edit applied to only one is exactly how E-1 stayed alive.
# ---------------------------------------------------------------------------


class TestBuildOptionsCarrySampling:
    @pytest.mark.parametrize("options_cls", [AgentBuildOptions, RouterBuildOptions])
    def test_options_have_sampling_field(self, options_cls):
        assert "sampling" in {f.name for f in fields(options_cls)}

    @pytest.mark.parametrize(
        "module_path",
        [
            "Agent.worker_agent.llm.llm_wrapper",
            "Agent.router_agent.llm.llm_wrapper",
            "Agent.worker_agent.llm.openai_client",
            "Agent.router_agent.llm.openai_client",
            "Agent.worker_agent.llm.anthropic_client",
            "Agent.router_agent.llm.anthropic_client",
            "Agent.worker_agent.llm.base",
            "Agent.router_agent.llm.base",
        ],
    )
    def test_both_stacks_accept_sampling(self, module_path):
        """The two LLM stacks are near-duplicates; a one-sided edit silently
        leaves the other provider path broken."""
        import importlib

        mod = importlib.import_module(module_path)
        clients = [
            obj
            for name, obj in vars(mod).items()
            if inspect.isclass(obj)
            and name.endswith(("Client", "ClientBase"))
            and obj.__module__ == module_path
        ]
        assert clients, f"no client class found in {module_path}"
        for cls in clients:
            assert "sampling" in inspect.signature(cls.__init__).parameters, cls


class TestA2ALayerCarriesSampling:
    def test_router_agent_builds_sampling_from_its_own_attrs(self):
        from a2a.coordinator.router import RouterAgent

        r = RouterAgent.__new__(RouterAgent)
        r._temperature = 0.42
        r._seed = 99
        got = r._sampling()
        # Compared field-wise, not with ==: router_agent and worker_agent each
        # define their own SamplingParams class, so dataclass equality across
        # the two stacks is always False no matter what the values are.
        assert (got.temperature, got.top_p, got.seed) == (0.42, None, 99)

    def test_the_two_stacks_have_distinct_sampling_classes(self):
        """Documents a real trap: the duplicated stacks mean an instance from one
        never equals an instance from the other. Any code comparing across them
        is wrong even when the numbers match."""
        from Agent.router_agent.schema import SamplingParams as RouterSampling
        from Agent.worker_agent.schema import SamplingParams as WorkerSampling

        assert RouterSampling is not WorkerSampling
        assert RouterSampling(temperature=0.5) != WorkerSampling(temperature=0.5)

    @pytest.mark.parametrize(
        "dotted,param",
        [
            ("a2a.coordinator.router:RouterAgent", "seed"),
            ("a2a.coordinator.verifier:VerifierAgent", "seed"),
            ("a2a.worker.agent_adapter:AgentAdapter", "seed"),
            ("a2a.worker.a2a_server:create_worker_a2a_server", "seed"),
            ("a2a.coordinator.server:CoordinatorServer", "router_seed"),
            ("a2a.coordinator.server:CoordinatorServer", "verifier_seed"),
            ("a2a.coordinator.server:create_server", "router_seed"),
            ("a2a.coordinator.server:create_server", "verifier_seed"),
        ],
    )
    def test_seed_is_accepted_at_each_a2a_entry_point(self, dotted, param):
        import importlib

        mod_name, obj_name = dotted.split(":")
        obj = getattr(importlib.import_module(mod_name), obj_name)
        target = obj.__init__ if inspect.isclass(obj) else obj
        assert param in inspect.signature(target).parameters

    def test_agent_adapter_forwards_temperature_into_build_options(self):
        """This exact boundary is where the value used to die: the adapter stored
        `self._temperature` and then built AgentBuildOptions without it."""
        from a2a.worker.agent_adapter import AgentAdapter

        adapter = AgentAdapter(temperature=0.333, seed=33, provider="openai")
        assert adapter._agent_opts.sampling == SamplingParams(
            temperature=0.333, seed=33
        )


# ---------------------------------------------------------------------------
# sar_orch: the layer that actually caused E-1, by hardcoding past the params.
# ---------------------------------------------------------------------------


class TestSarOrchLayer:
    def test_coordinator_stores_sampling(self):
        from sar_orch.coordinator import SARCoordinator

        c = SARCoordinator(temperature=0.123, verifier_temperature=0.077, llm_seed=456)
        assert (c._temperature, c._verifier_temperature, c._llm_seed) == (
            0.123,
            0.077,
            456,
        )

    def test_worker_stores_sampling(self):
        from sar_orch.worker import SARWorker

        w = SARWorker(
            worker_id="Alice",
            agent_name="Alice",
            agent_idx=0,
            barrier=None,
            temperature=0.123,
            llm_seed=456,
        )
        assert (w._temperature, w._llm_seed) == (0.123, 456)

    def test_defaults_are_unchanged(self):
        """T1 was plumbing only. Changing a default would alter behaviour in the
        same commit, which the design forbids (one variable at a time)."""
        from sar_orch.coordinator import SARCoordinator
        from sar_orch.worker import SARWorker

        c = SARCoordinator()
        w = SARWorker(worker_id="A", agent_name="A", agent_idx=0, barrier=None)
        assert (c._temperature, c._verifier_temperature, c._llm_seed) == (0.7, 0.3, None)
        assert (w._temperature, w._llm_seed) == (0.7, None)

    def test_no_hardcoded_temperature_remains_in_sar_orch(self):
        """Guards the specific regression: a literal reintroduced at the
        create_*_server call would re-sever the chain while every test above
        still passed."""
        from pathlib import Path

        import sar_orch

        root = Path(sar_orch.__file__).parent
        offenders = []
        for rel in ("coordinator.py", "worker.py"):
            for lineno, line in enumerate(
                (root / rel).read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # A literal float assigned to a temperature kwarg is the bug.
                # MapAgent's ChatOpenAI(temperature=0.0) is excluded: langchain
                # injects it for real, so it was never part of E-1.
                if "temperature=0.7" in stripped or "temperature=0.3" in stripped:
                    offenders.append(f"{rel}:{lineno}: {stripped}")
        assert not offenders, "hardcoded temperature reintroduced:\n" + "\n".join(
            offenders
        )

    def test_experiment_exposes_both_seeds_separately(self):
        """`--seed` seeds the scene; `--llm-seed` seeds sampling. Merging them
        would make the variance baseline uninterpretable."""
        from sar_orch.experiment import run_experiment

        params = inspect.signature(run_experiment).parameters
        assert params["seed"].default == 42
        assert params["temperature"].default == 0.7
        assert params["llm_seed"].default is None

    def test_metadata_records_sampling(self):
        """A run must be able to answer "what sampling did I use?" after the
        fact -- otherwise the variance baseline cannot be audited."""
        from pathlib import Path

        import sar_orch

        src = (Path(sar_orch.__file__).parent / "experiment.py").read_text(
            encoding="utf-8"
        )
        for key in ("temperature", "llm_seed", "llm_seed_supported"):
            assert f'metadata["{key}"]' in src, key

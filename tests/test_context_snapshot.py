"""Tests for ContextManager task snapshot storage (ContextSnapshotV2).

ContextSnapshotV2 saves only messages + ContextSessionCursor (keyed by
``(scope_id, viewer_id)``) + LoadedSkillRef{name, source_relative_path,
content_sha256}; pinned / RuntimeState are never serialized.  On restore skills
are reloaded only from the configured skill root when the path + digest match
(absolute / ``..`` traversal refs are rejected), otherwise the
SKILL_RELOAD_REQUIRED marker is rendered.
"""

import json

from Agent.worker_agent.context import (
    ContextManager,
    LoadedSkillRef,
    SKILL_RELOAD_REQUIRED,
    _sha256_hex,
)
from Agent.worker_agent.schema import Message
from Agent.worker_agent.tools.skill_loader import SkillLoader


def _write_skill(tmp_path, name: str, body: str):
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test {name}\n---\n\n# Skill: {name}\n\n{body}",
        encoding="utf-8",
    )
    return tmp_path


def _rendered_skill(tmp_path, name: str, body: str) -> str:
    skills_root = _write_skill(tmp_path, name, body)
    loader = SkillLoader(skills_dir=str(skills_root))
    loader.discover_skills()
    skill = loader.get_skill(name)
    assert skill is not None
    return skill.to_prompt()


def test_save_and_load_snapshot():
    ctx = ContextManager()
    msgs = [
        Message(role="system", content="prompt"),
        Message(role="user", content="go"),
        Message(role="assistant", content="ok"),
    ]
    ctx.save_snapshot("task-1", msgs)
    loaded = ctx.load_snapshot("task-1")
    assert loaded is not None
    assert len(loaded) == 3
    assert loaded[0].role == "system"
    assert loaded[2].content == "ok"


def test_load_snapshot_returns_none_if_not_saved():
    ctx = ContextManager()
    assert ctx.load_snapshot("nonexistent") is None


def test_load_snapshot_pops_after_read():
    """load_snapshot 是一次性的——第二次返回 None。"""
    ctx = ContextManager()
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])
    first = ctx.load_snapshot("task-1")
    second = ctx.load_snapshot("task-1")
    assert first is not None
    assert second is None


def test_snapshots_isolated_by_task_id():
    ctx = ContextManager()
    ctx.save_snapshot("task-a", [Message(role="user", content="a")])
    ctx.save_snapshot("task-b", [Message(role="user", content="b")])
    loaded_a = ctx.load_snapshot("task-a")
    loaded_b = ctx.load_snapshot("task-b")
    assert loaded_a[0].content == "a"
    assert loaded_b[0].content == "b"


def test_save_snapshot_does_not_mutate_original():
    """保存的快照应该是副本，修改原始不影响快照。"""
    ctx = ContextManager()
    msgs = [Message(role="user", content="original")]
    ctx.save_snapshot("task-1", msgs)
    msgs.append(Message(role="assistant", content="appended"))
    loaded = ctx.load_snapshot("task-1")
    assert len(loaded) == 1
    assert loaded[0].content == "original"


# ---------------------------------------------------------------------------
# ContextSnapshotV2 semantics
# ---------------------------------------------------------------------------


def test_snapshot_does_not_serialize_pinned_or_runtime_state(tmp_path):
    ctx = ContextManager(log_dir=tmp_path)
    ctx.pinned["position"] = (1, 2, 0)
    ctx.observe(
        "navigate_to", "[GPS] Position: (1, 2, 0) | Inventory: [] | Step: 4", True
    )

    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])
    payload = json.loads(
        (tmp_path / "snapshot_task-1.json").read_text(encoding="utf-8")
    )

    assert payload["version"] == 2
    assert "pinned" not in payload
    assert "runtime_state" not in payload
    assert "payload" not in payload
    assert set(payload) == {"version", "cursor", "loaded_skills", "messages"}


def test_snapshot_restores_messages_only_without_pinned(tmp_path):
    ctx = ContextManager(log_dir=tmp_path)
    ctx.pinned["position"] = (9, 9, 0)
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])

    fresh = ContextManager(log_dir=tmp_path)
    loaded = fresh.load_snapshot("task-1")
    assert loaded is not None
    assert loaded[0].content == "hi"
    # ContextSnapshotV2 never restores pinned from a snapshot.
    assert fresh.pinned == {}


def test_snapshot_stores_skill_refs_not_raw_content(tmp_path):
    rendered = _rendered_skill(tmp_path, "navigation", "body-one")

    ctx = ContextManager(log_dir=tmp_path, skills_dir=tmp_path)
    ctx.on_skill_loaded("navigation", rendered)
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])

    payload = json.loads(
        (tmp_path / "snapshot_task-1.json").read_text(encoding="utf-8")
    )
    refs = payload["loaded_skills"]
    assert len(refs) == 1
    ref = refs[0]
    assert set(ref) == {"name", "source_relative_path", "content_sha256"}
    assert ref["name"] == "navigation"
    assert ref["source_relative_path"] == "navigation/SKILL.md"
    assert rendered not in payload["messages"]


def test_load_snapshot_restores_matching_skill(tmp_path):
    rendered = _rendered_skill(tmp_path, "navigation", "body-one")

    ctx = ContextManager(log_dir=tmp_path, skills_dir=tmp_path)
    ctx.on_skill_loaded("navigation", rendered)
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])

    fresh = ContextManager(log_dir=tmp_path, skills_dir=tmp_path)
    fresh.load_snapshot("task-1")
    assert fresh._loaded_skills["navigation"] == rendered
    assert fresh._loaded_skill_refs["navigation"].name == "navigation"


def test_load_snapshot_marks_reload_required_on_hash_mismatch(tmp_path):
    rendered = _rendered_skill(tmp_path, "navigation", "body-one")

    ctx = ContextManager(log_dir=tmp_path, skills_dir=tmp_path)
    ctx.on_skill_loaded("navigation", rendered)
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])

    # Mutate the skill source so the stored digest no longer matches.
    _write_skill(tmp_path, "navigation", "body-CHANGED")

    fresh = ContextManager(log_dir=tmp_path, skills_dir=tmp_path)
    fresh.load_snapshot("task-1")
    assert fresh._loaded_skills["navigation"] == SKILL_RELOAD_REQUIRED


def test_load_snapshot_marks_reload_required_without_skills_dir(tmp_path):
    rendered = _rendered_skill(tmp_path, "navigation", "body-one")

    ctx = ContextManager(log_dir=tmp_path, skills_dir=tmp_path)
    ctx.on_skill_loaded("navigation", rendered)
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])

    # Restore without a configured skill root → cannot verify → reload required.
    fresh = ContextManager(log_dir=tmp_path, skills_dir=None)
    fresh.load_snapshot("task-1")
    assert fresh._loaded_skills["navigation"] == SKILL_RELOAD_REQUIRED


def test_router_snapshot_uses_context_snapshot_v2(tmp_path):
    from Agent.router_agent.context import CoordinatorContextManager

    ctx = CoordinatorContextManager(log_dir=tmp_path)
    ctx.pinned["x"] = 1
    ctx.save_snapshot("task-r", [Message(role="user", content="hi")])
    payload = json.loads(
        (tmp_path / "snapshot_task-r.json").read_text(encoding="utf-8")
    )
    assert payload["version"] == 2
    assert "pinned" not in payload
    assert "loaded_skills" in payload
    assert "cursor" in payload


# ---------------------------------------------------------------------------
# ContextSession temporal cursor (keyed by (scope_id, viewer_id))
# ---------------------------------------------------------------------------


def test_next_cursor_is_monotonic_and_namespace_scoped():
    ctx = ContextManager()
    assert ctx.get_cursor("scope-1", "viewer-a") == 0
    assert ctx.next_cursor("scope-1", "viewer-a") == 1
    assert ctx.next_cursor("scope-1", "viewer-a") == 2
    assert ctx.next_cursor("scope-1", "viewer-a") == 3
    assert ctx.get_cursor("scope-1", "viewer-a") == 3
    # A different scope/viewer namespace is independent (never inferred).
    assert ctx.get_cursor("scope-2", "viewer-a") == 0
    assert ctx.get_cursor("scope-1", "viewer-b") == 0


def test_snapshot_persists_and_restores_cursor_roundtrip(tmp_path):
    ctx = ContextManager(log_dir=tmp_path)
    ctx.next_cursor("scope-1", "viewer-a")
    ctx.next_cursor("scope-1", "viewer-a")
    ctx.next_cursor("scope-1", "viewer-a")
    ctx.save_snapshot(
        "task-1",
        [Message(role="user", content="hi")],
        scope_id="scope-1",
        viewer_id="viewer-a",
    )

    payload = json.loads(
        (tmp_path / "snapshot_task-1.json").read_text(encoding="utf-8")
    )
    assert payload["cursor"] == {
        "scope_id": "scope-1",
        "viewer_id": "viewer-a",
        "sequence": 3,
    }

    fresh = ContextManager(log_dir=tmp_path)
    assert fresh.load_snapshot("task-1") is not None
    assert fresh.get_cursor("scope-1", "viewer-a") == 3
    # Restored cursor stays namespace-scoped; other scopes remain at 0.
    assert fresh.get_cursor("scope-other", "viewer-a") == 0


def test_snapshot_in_memory_cursor_roundtrip():
    """In-memory snapshot (no log_dir) carries and restores the cursor."""
    ctx = ContextManager()
    ctx.next_cursor("scope-1", "viewer-a")
    ctx.next_cursor("scope-1", "viewer-a")
    ctx.save_snapshot(
        "task-1",
        [Message(role="user", content="hi")],
        scope_id="scope-1",
        viewer_id="viewer-a",
    )
    loaded = ctx.load_snapshot("task-1")
    assert loaded is not None
    assert ctx.get_cursor("scope-1", "viewer-a") == 2


def test_snapshot_missing_cursor_starts_at_zero(tmp_path):
    """Legacy v2 payload without a cursor resumes at sequence=0 (no cross-scope inference)."""
    payload = {
        "version": 2,
        "loaded_skills": [],
        "messages": [Message(role="user", content="hi").model_dump()],
    }
    (tmp_path / "snapshot_legacy.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    ctx = ContextManager(log_dir=tmp_path)
    assert ctx.load_snapshot("legacy") is not None
    assert ctx.get_cursor("scope-1", "viewer-a") == 0


def test_snapshot_cursor_payload_excludes_pinned_and_runtime(tmp_path):
    ctx = ContextManager(log_dir=tmp_path)
    ctx.pinned["position"] = (1, 2, 0)
    ctx.observe(
        "navigate_to", "[GPS] Position: (1, 2, 0) | Inventory: [] | Step: 4", True
    )
    ctx.next_cursor("scope-1", "viewer-a")
    ctx.save_snapshot(
        "task-1",
        [Message(role="user", content="hi")],
        scope_id="scope-1",
        viewer_id="viewer-a",
    )
    payload = json.loads(
        (tmp_path / "snapshot_task-1.json").read_text(encoding="utf-8")
    )
    assert set(payload) == {"version", "cursor", "loaded_skills", "messages"}
    assert "pinned" not in payload
    assert "runtime_state" not in payload
    assert "payload" not in payload


def test_router_context_cursor_roundtrip(tmp_path):
    from Agent.router_agent.context import CoordinatorContextManager

    ctx = CoordinatorContextManager(log_dir=tmp_path)
    ctx.next_cursor("scope-r", "system")
    ctx.next_cursor("scope-r", "system")
    ctx.save_snapshot(
        "task-r",
        [Message(role="user", content="hi")],
        scope_id="scope-r",
        viewer_id="system",
    )
    fresh = CoordinatorContextManager(log_dir=tmp_path)
    fresh.load_snapshot("task-r")
    assert fresh.get_cursor("scope-r", "system") == 2


# ---------------------------------------------------------------------------
# Skill restore path confinement
# ---------------------------------------------------------------------------


def _write_external_skill(root, name: str, body: str) -> str:
    """Write a skill OUTSIDE ``root`` and return its rendered content."""
    external = root / "external"
    skill_dir = external / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: ext\n---\n\n# Skill: {name}\n\n{body}",
        encoding="utf-8",
    )
    loader = SkillLoader(skills_dir=str(external))
    loader.discover_skills()
    skill = loader.get_skill(name)
    assert skill is not None
    return skill.to_prompt()


def test_out_of_root_skill_source_stores_no_absolute_path(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    rendered = _write_external_skill(tmp_path, "nav", "outside")

    ctx = ContextManager(log_dir=tmp_path, skills_dir=root)
    ctx.on_skill_loaded("nav", rendered)
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])

    payload = json.loads(
        (tmp_path / "snapshot_task-1.json").read_text(encoding="utf-8")
    )
    ref = payload["loaded_skills"][0]
    # Out-of-root source must not persist an absolute path — no usable path.
    assert ref["source_relative_path"] == ""
    assert not str(ref["source_relative_path"]).startswith("/")

    # Restore cannot reach the external skill → reload required.
    fresh = ContextManager(log_dir=tmp_path, skills_dir=root)
    fresh.load_snapshot("task-1")
    assert fresh._loaded_skills["nav"] == SKILL_RELOAD_REQUIRED


def test_reload_rejects_parent_traversal_ref(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    rendered = _write_external_skill(tmp_path, "evil", "pwned")

    ctx = ContextManager(skills_dir=root)
    malicious = LoadedSkillRef(
        name="evil",
        source_relative_path="../external/evil/SKILL.md",
        content_sha256=_sha256_hex(rendered),
    )
    # Path is confined to root; the traversal is rejected even though the
    # digest would match the external file.
    assert ctx._reload_skill_content(malicious) is None


def test_reload_rejects_absolute_ref(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    rendered = _write_external_skill(tmp_path, "evil", "pwned")
    external_path = tmp_path / "external" / "evil" / "SKILL.md"

    ctx = ContextManager(skills_dir=root)
    absolute = LoadedSkillRef(
        name="evil",
        source_relative_path=str(external_path),
        content_sha256=_sha256_hex(rendered),
    )
    assert ctx._reload_skill_content(absolute) is None


def test_load_snapshot_marks_reload_required_for_malicious_ref(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    rendered = _write_external_skill(tmp_path, "evil", "pwned")

    payload = {
        "version": 2,
        "cursor": {"scope_id": "s", "viewer_id": "v", "sequence": 1},
        "loaded_skills": [
            {
                "name": "evil",
                "source_relative_path": "../external/evil/SKILL.md",
                "content_sha256": _sha256_hex(rendered),
            }
        ],
        "messages": [Message(role="user", content="hi").model_dump()],
    }
    (tmp_path / "snapshot_mal.json").write_text(json.dumps(payload), encoding="utf-8")

    ctx = ContextManager(log_dir=tmp_path, skills_dir=root)
    assert ctx.load_snapshot("mal") is not None
    assert ctx._loaded_skills["evil"] == SKILL_RELOAD_REQUIRED


def test_reload_allows_nested_skill_within_root(tmp_path):
    root = tmp_path / "skills"
    (root / "sub" / "nav").mkdir(parents=True)
    (root / "sub" / "nav" / "SKILL.md").write_text(
        "---\nname: nav\ndescription: x\n---\n\n# Skill: nav\n\nnested",
        encoding="utf-8",
    )
    loader = SkillLoader(skills_dir=str(root))
    loader.discover_skills()
    skill = loader.get_skill("nav")
    assert skill is not None
    rendered = skill.to_prompt()

    ctx = ContextManager(log_dir=tmp_path, skills_dir=root)
    ctx.on_skill_loaded("nav", rendered)
    ctx.save_snapshot("task-1", [Message(role="user", content="hi")])
    payload = json.loads(
        (tmp_path / "snapshot_task-1.json").read_text(encoding="utf-8")
    )
    assert payload["loaded_skills"][0]["source_relative_path"] == "sub/nav/SKILL.md"

    fresh = ContextManager(log_dir=tmp_path, skills_dir=root)
    fresh.load_snapshot("task-1")
    assert fresh._loaded_skills["nav"] == rendered


def test_loaded_skill_ref_roundtrip():
    ref = LoadedSkillRef(
        name="nav", source_relative_path="navigation/SKILL.md", content_sha256="abc"
    )
    assert LoadedSkillRef.from_dict(ref.to_dict()) == ref
    assert LoadedSkillRef.from_dict({}) == LoadedSkillRef(name="")


def test_skill_loader_canonical_ref(tmp_path):
    _write_skill(tmp_path, "navigation", "body-one")
    loader = SkillLoader(skills_dir=str(tmp_path))
    loader.discover_skills()
    skill = loader.get_skill("navigation")
    assert skill is not None
    ref = skill.canonical_ref(tmp_path)
    assert set(ref) == {"name", "source_relative_path", "content_sha256"}
    assert ref["name"] == "navigation"
    assert ref["source_relative_path"] == "navigation/SKILL.md"
    assert len(ref["content_sha256"]) == 64

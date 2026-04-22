from __future__ import annotations

import importlib.util
import os
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "collab.py"
SPEC = importlib.util.spec_from_file_location("collab", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
collab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collab)


def test_record_role_sync_persists_checkpoint_metadata(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    collab.command_new_task(
        root,
        title="Builder task",
        created_by="architect",
        assigned_role="builder",
        source_plan=None,
        branch="feat/builder-task",
        files=[],
        acceptance=[],
        note=None,
    )

    state = collab.record_role_sync(
        root,
        role="builder",
        actor="builder",
        task_id="TASK-001",
        branch="feat/builder-task",
        note="starting implementation",
        inbox_mtime=0.0,
    )

    role_state = state["roles"]["builder"]
    assert role_state["current_task_id"] == "TASK-001"
    assert role_state["queue_depth"] == 1
    assert role_state["last_sync_by"] == "builder"
    assert role_state["last_sync_task_id"] == "TASK-001"
    assert role_state["last_sync_branch"] == "feat/builder-task"
    assert role_state["last_sync_note"] == "starting implementation"
    assert role_state["last_inbox_mtime"] == 0.0


def test_command_sync_surfaces_task_and_inbox_updates_since_last_sync(
    tmp_path,
    capsys,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    collab.command_sync(
        root,
        role="builder",
        actor="builder",
        task_id=None,
        branch=None,
        note="idle",
        max_age_seconds=600,
    )
    capsys.readouterr()

    collab.command_new_task(
        root,
        title="Queued task",
        created_by="architect",
        assigned_role="builder",
        source_plan=None,
        branch="feat/queued-task",
        files=["src/example.py"],
        acceptance=["tests pass"],
        note="Start here",
    )
    task = collab.load_task(root, "TASK-001")
    task["updated_at"] = "2099-01-01T00:00:00+00:00"
    collab.write_json(collab.task_file(root, "TASK-001"), task)

    collab.command_message(
        root,
        to_role="builder",
        from_actor="architect",
        subject="Queued task ready",
        body="Please pick up TASK-001 next.",
    )
    future_epoch = 4_102_444_800
    os.utime(collab.inbox_path(root, "builder"), (future_epoch, future_epoch))

    collab.command_sync(
        root,
        role="builder",
        actor="builder",
        task_id="TASK-001",
        branch="feat/queued-task",
        note="reviewing queue",
        max_age_seconds=600,
    )
    output = capsys.readouterr().out

    assert "task updates since last sync:" in output
    assert "TASK-001 [proposed] Queued task" in output
    assert "inbox updated since last sync" in output


def test_command_status_shows_role_sync_metadata(tmp_path, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    collab.record_role_sync(
        root,
        role="builder",
        actor="builder",
        task_id="TASK-007",
        branch="feat/remediation-rollout-models",
        note="implementing rollout models",
        inbox_mtime=0.0,
    )

    collab.command_status(root)
    output = capsys.readouterr().out

    assert "last_sync_by=builder" in output
    assert "last_sync_task_id=TASK-007" in output
    assert "last_sync_branch=feat/remediation-rollout-models" in output


def test_state_changing_commands_auto_sync_actor_role(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    collab.command_new_task(
        root,
        title="Queued task",
        created_by="architect",
        assigned_role="builder",
        source_plan=None,
        branch="feat/queued-task",
        files=[],
        acceptance=[],
        note=None,
    )

    state = collab.read_json(collab.state_path(root))
    architect_state = state["roles"]["architect"]
    assert architect_state["last_sync_by"] == "architect"
    assert architect_state["last_sync_task_id"] == "TASK-001"
    assert architect_state["last_sync_branch"] == "feat/queued-task"
    assert architect_state["last_sync_note"] == "auto: created TASK-001"

    collab.command_claim(root, task_id="TASK-001", actor="builder")
    state = collab.read_json(collab.state_path(root))
    builder_state = state["roles"]["builder"]
    assert builder_state["last_sync_by"] == "builder"
    assert builder_state["last_sync_task_id"] == "TASK-001"
    assert builder_state["last_sync_branch"] == "feat/queued-task"
    assert builder_state["last_sync_note"] == "auto: claimed TASK-001"


def test_command_next_includes_handoff_context(tmp_path, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    collab.command_new_task(
        root,
        title="Queued task",
        created_by="architect",
        assigned_role="builder",
        source_plan="docs/plan.md",
        branch="feat/queued-task",
        files=["src/example.py"],
        acceptance=["tests pass"],
        note="Start here",
    )
    collab.command_claim(root, task_id="TASK-001", actor="builder")
    collab.command_handoff(
        root,
        task_id="TASK-001",
        actor="builder",
        to_role="architect",
        summary="Ready for review",
        branch="feat/queued-task",
        pr_url="https://github.com/example/repo/pull/1",
        commit="abc1234",
    )

    collab.command_next(root, "architect")
    output = capsys.readouterr().out

    assert "handoff_pr_url: https://github.com/example/repo/pull/1" in output
    assert "handoff_summary: Ready for review" in output
    assert "handoff_commit: abc1234" in output

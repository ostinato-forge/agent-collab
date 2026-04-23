from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "collab.py"
SPEC = importlib.util.spec_from_file_location("collab", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
collab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collab)


def create_task(
    root: Path,
    *,
    title: str = "Queued task",
    created_by: str = "architect",
    assigned_role: str = "builder",
    branch: str = "feat/queued-task",
    verification_cmd: list[str] | None = None,
) -> None:
    collab.command_new_task(
        root,
        title=title,
        created_by=created_by,
        assigned_role=assigned_role,
        source_plan=None,
        branch=branch,
        files=[],
        acceptance=[],
        note=None,
        verification_cmd=verification_cmd,
    )


def stub_git_context(monkeypatch, *, branch: str, head: str) -> None:
    monkeypatch.setattr(
        collab,
        "git_context",
        lambda _root: {"branch": branch, "head": head, "error": None},
    )


def test_record_role_sync_persists_checkpoint_metadata(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    create_task(root, title="Builder task", branch="feat/builder-task")

    state = collab.record_role_sync(
        root,
        role="builder",
        actor="builder",
        task_id="TASK-001",
        branch="feat/builder-task",
        head=None,
        note="starting implementation",
        inbox_mtime=0.0,
    )

    role_state = state["roles"]["builder"]
    assert role_state["current_task_id"] == "TASK-001"
    assert role_state["queue_depth"] == 1
    assert role_state["last_sync_by"] == "builder"
    assert role_state["last_sync_task_id"] == "TASK-001"
    assert role_state["last_sync_branch"] == "feat/builder-task"
    assert role_state["last_sync_source"] == "manual"
    assert role_state["last_sync_reason"] == "starting implementation"
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
        max_age_seconds=collab.PEER_STALE_THRESHOLD_SECONDS,
        check_stale=False,
        stale_threshold_seconds=collab.STALE_THRESHOLD_SECONDS,
    )
    capsys.readouterr()

    create_task(root)
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
        max_age_seconds=collab.PEER_STALE_THRESHOLD_SECONDS,
        check_stale=False,
        stale_threshold_seconds=collab.STALE_THRESHOLD_SECONDS,
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
        head=None,
        note="implementing rollout models",
        inbox_mtime=0.0,
    )

    collab.command_status(root)
    output = capsys.readouterr().out

    assert "last_sync_by=builder" in output
    assert "last_sync_task_id=TASK-007" in output
    assert "last_sync_branch=feat/remediation-rollout-models" in output
    assert "last_sync_head=" in output
    assert "last_sync_source=manual" in output
    assert "sync_status=" in output


def test_state_changing_commands_auto_sync_actor_role(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    create_task(root)

    task = collab.load_task(root, "TASK-001")
    assert task["schema_version"] == collab.TASK_SCHEMA_VERSION

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
        verification_cmd=None,
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
        skip_verify=False,
        allow_unsynced=False,
    )

    collab.command_next(root, "architect")
    output = capsys.readouterr().out

    assert "handoff_pr_url: https://github.com/example/repo/pull/1" in output
    assert "handoff_summary: Ready for review" in output
    assert "handoff_commit: abc1234" in output


def test_invalid_transitions_are_rejected(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    create_task(root, title="Approved path", branch="feat/approved-path")
    collab.command_claim(root, task_id="TASK-001", actor="builder")
    collab.command_handoff(
        root,
        task_id="TASK-001",
        actor="builder",
        to_role="architect",
        summary="Ready",
        branch="feat/approved-path",
        pr_url=None,
        commit=None,
        skip_verify=False,
        allow_unsynced=False,
    )
    collab.command_review(
        root,
        task_id="TASK-001",
        actor="architect",
        decision="approve",
        summary="Looks good",
        findings=[],
        allow_unsynced=False,
    )

    with pytest.raises(SystemExit, match="approved -> in_progress"):
        collab.command_claim(root, task_id="TASK-001", actor="builder")

    create_task(root, title="Blocked path", branch="feat/blocked-path")
    collab.command_claim(root, task_id="TASK-002", actor="builder")
    collab.command_handoff(
        root,
        task_id="TASK-002",
        actor="builder",
        to_role="architect",
        summary="Needs decision",
        branch="feat/blocked-path",
        pr_url=None,
        commit=None,
        skip_verify=False,
        allow_unsynced=False,
    )
    collab.command_review(
        root,
        task_id="TASK-002",
        actor="architect",
        decision="blocked",
        summary="Blocked on upstream dependency",
        findings=[],
        allow_unsynced=False,
    )

    with pytest.raises(SystemExit, match="blocked -> done"):
        collab.command_complete(
            root,
            task_id="TASK-002",
            actor="builder",
            summary="cannot",
            allow_unsynced=False,
        )


def test_task_ids_are_case_insensitive_for_cli_commands(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    create_task(root, branch="feat/case-test")

    collab.command_claim(root, task_id="task-001", actor="builder")

    task = collab.load_task(root, "TASK-001")
    state = collab.read_json(collab.state_path(root))
    assert task["status"] == "in_progress"
    assert state["roles"]["builder"]["last_sync_task_id"] == "TASK-001"


def test_command_verify_records_verification_result(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    create_task(
        root,
        title="Verification task",
        branch="feat/verify-task",
        verification_cmd=["pytest", "-q"],
    )
    collab.command_claim(root, task_id="TASK-001", actor="builder")

    def fake_run_verification(_root: Path, task: dict[str, object]) -> dict[str, object]:
        return {
            "passed": True,
            "ran_at": collab.utc_now(),
            "exit_code": 0,
            "branch": task["branch"],
            "head": "abc123",
            "command": ["pytest", "-q"],
            "output": "all green",
        }

    monkeypatch.setattr(collab, "run_verification", fake_run_verification)

    result = collab.command_verify(root, task_id="TASK-001", actor="builder")

    task = collab.load_task(root, "TASK-001")
    assert result == 0
    assert task["verification"]["passed"] is True
    assert task["verification"]["output"] == "all green"
    assert any(item["event"] == "verify" for item in task["history"])


def test_cli_new_task_accepts_verify_cmd_string(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--root",
            str(root),
            "new-task",
            "CLI verify cmd",
            "--created-by",
            "architect",
            "--branch",
            "feat/cli-verify",
            "--verify-cmd",
            "python3 -c \"print('ok')\"",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    task = collab.load_task(root, "TASK-001")
    assert task["verification_cmd"] == ["python3", "-c", "print('ok')"]


def test_handoff_rejects_failed_verification(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    create_task(
        root,
        title="Failing verification task",
        branch="feat/failing-task",
        verification_cmd=["pytest"],
    )
    collab.command_claim(root, task_id="TASK-001", actor="builder")

    def fake_run_verification(_root: Path, task: dict[str, object]) -> dict[str, object]:
        return {
            "passed": False,
            "ran_at": collab.utc_now(),
            "exit_code": 1,
            "branch": task["branch"],
            "head": "deadbeef",
            "command": ["pytest"],
            "output": "branch mismatch: expected feat/failing-task, got wrong-branch",
        }

    monkeypatch.setattr(collab, "run_verification", fake_run_verification)

    with pytest.raises(SystemExit, match="verification failed for TASK-001"):
        collab.command_handoff(
            root,
            task_id="TASK-001",
            actor="builder",
            to_role="architect",
            summary="Done",
            branch="feat/failing-task",
            pr_url=None,
            commit=None,
            skip_verify=False,
            allow_unsynced=False,
        )

    task = collab.load_task(root, "TASK-001")
    assert task["status"] == "in_progress"
    assert task["verification"]["passed"] is False
    assert "branch mismatch" in task["verification"]["output"]
    assert any(item["event"] == "verify" for item in task["history"])


def test_check_stale_flags_once_and_notifies_inbox(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)
    create_task(root)
    collab.command_claim(root, task_id="TASK-001", actor="builder")

    task = collab.load_task(root, "TASK-001")
    task["last_activity_at"] = "2000-01-01T00:00:00+00:00"
    collab.write_json(collab.task_file(root, "TASK-001"), task)

    collab.command_check_stale(root, actor="architect", threshold_seconds=60)
    collab.command_check_stale(root, actor="architect", threshold_seconds=60)

    task = collab.load_task(root, "TASK-001")
    inbox = collab.inbox_path(root, "architect").read_text(encoding="utf-8")
    stale_events = [item for item in task["history"] if item["event"] == "stale"]

    assert task["stale_flagged_at"] is not None
    assert task["stale_reason"].startswith("no activity for more than 60s")
    assert len(stale_events) == 1
    assert inbox.count("TASK-001 [in_progress] Queued task") == 1


def test_parallel_new_task_allocates_unique_ids(tmp_path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    collab.command_init(root)

    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(MODULE_PATH),
                "--root",
                str(root),
                "new-task",
                f"Task {index}",
                "--created-by",
                "architect",
                "--branch",
                f"feat/task-{index}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(1, 7)
    ]

    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)

    tasks = collab.load_tasks(root)
    task_ids = [task["task_id"] for task in tasks]
    state = collab.read_json(collab.state_path(root))

    assert len(task_ids) == 6
    assert len(set(task_ids)) == 6
    assert task_ids == [f"TASK-{index:03d}" for index in range(1, 7)]
    assert state["last_task_number"] == 6


def test_enable_writes_agent_config_and_installs_hooks(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    stub_git_context(monkeypatch, branch="feat/runtime", head="abc123")
    monkeypatch.setattr(collab, "git_hooks_dir", lambda _root: hooks_dir)

    collab.command_init(root)
    result = collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="warn",
        install_hooks=True,
    )

    assert result == 0
    agent = collab.load_agent_config(root)
    manifest = collab.load_hooks_manifest(root)
    assert agent is not None
    assert agent["role"] == "builder"
    assert agent["enforcement_mode"] == "warn"
    assert collab.role_runtime_path(root, "builder").exists()
    assert collab.load_active_runtime_role(root) == "builder"
    assert manifest is not None
    assert set(manifest["hooks"]) == set(collab.HOOK_EVENTS)
    assert manifest["active_role"] == "builder"
    hook_payload = (hooks_dir / "post-commit").read_text(encoding="utf-8")
    assert collab.HOOK_MARKER in hook_payload
    assert '--role "builder"' in hook_payload


def test_git_event_records_hook_sync_metadata(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stub_git_context(monkeypatch, branch="feat/hook-sync", head="deadbeef")

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="warn",
        install_hooks=False,
    )

    result = collab.command_git_event(root, event="post-commit")

    state = collab.read_json(collab.state_path(root))
    role_state = state["roles"]["builder"]
    assert result == 0
    assert role_state["last_sync_branch"] == "feat/hook-sync"
    assert role_state["last_sync_head"] == "deadbeef"
    assert role_state["last_sync_source"] == "hook"
    assert role_state["last_sync_reason"] == "post-commit"


def test_per_role_runtime_files_can_coexist(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stub_git_context(monkeypatch, branch="feat/multi-runtime", head="abc321")

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="architect",
        role="architect",
        peer="builder",
        enforcement_mode="warn",
        install_hooks=False,
    )
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="hard",
        install_hooks=False,
    )

    architect = collab.load_agent_config(root, role="architect")
    builder = collab.load_agent_config(root, role="builder")

    assert architect is not None
    assert builder is not None
    assert architect["actor"] == "architect"
    assert builder["actor"] == "builder"
    assert architect["enforcement_mode"] == "warn"
    assert builder["enforcement_mode"] == "hard"
    assert collab.load_active_runtime_role(root) == "builder"
    assert collab.role_runtime_path(root, "architect").exists()
    assert collab.role_runtime_path(root, "builder").exists()


def test_install_hooks_preserves_original_hook_across_reinstall(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    original_hook = hooks_dir / "post-commit"
    original_hook.write_text("#!/bin/sh\necho original-hook\n", encoding="utf-8")
    stub_git_context(monkeypatch, branch="feat/hook-preserve", head="abc999")
    monkeypatch.setattr(collab, "git_hooks_dir", lambda _root: hooks_dir)

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="warn",
        install_hooks=True,
    )
    collab.command_install_hooks(root)
    collab.command_uninstall_hooks(root)

    restored = original_hook.read_text(encoding="utf-8")
    assert "original-hook" in restored
    assert collab.HOOK_MARKER not in restored


def test_pre_push_warn_mode_reports_but_does_not_block(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stub_git_context(monkeypatch, branch="feat/pre-push-warn", head="fade123")

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="warn",
        install_hooks=False,
    )
    stub_git_context(monkeypatch, branch="feat/pre-push-warn-drift", head="fade124")
    result = collab.command_git_event(root, event="pre-push")
    captured = capsys.readouterr()

    assert result == 0
    assert "pre-push:" in captured.err
    assert "daemon heartbeat is missing" in captured.err


def test_pre_push_hard_mode_blocks_without_daemon(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stub_git_context(monkeypatch, branch="feat/pre-push-hard", head="fade456")

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="hard",
        install_hooks=False,
    )
    result = collab.command_git_event(root, event="pre-push")
    captured = capsys.readouterr()

    assert result == 1
    assert "daemon heartbeat is missing" in captured.err


def test_hard_mode_blocks_handoff_without_fresh_daemon(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stub_git_context(monkeypatch, branch="feat/hard-mode", head="beadfeed")

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="hard",
        install_hooks=False,
    )
    create_task(root, branch="feat/hard-mode")
    collab.command_claim(root, task_id="TASK-001", actor="builder")

    with pytest.raises(SystemExit, match="daemon heartbeat is missing"):
        collab.command_handoff(
            root,
            task_id="TASK-001",
            actor="builder",
            to_role="architect",
            summary="Ready",
            branch="feat/hard-mode",
            pr_url=None,
            commit=None,
            skip_verify=False,
            allow_unsynced=False,
        )

    collab.command_handoff(
        root,
        task_id="TASK-001",
        actor="builder",
        to_role="architect",
        summary="Ready",
        branch="feat/hard-mode",
        pr_url=None,
        commit=None,
        skip_verify=False,
        allow_unsynced=True,
    )
    task = collab.load_task(root, "TASK-001")
    assert task["handoff"]["sync_override"]["issues"] == ["daemon heartbeat is missing"]


def test_daemon_once_does_not_satisfy_hard_mode(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stub_git_context(monkeypatch, branch="feat/daemon-ok", head="c0ffee")

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="hard",
        install_hooks=False,
    )
    create_task(root, branch="feat/daemon-ok")
    collab.command_claim(root, task_id="TASK-001", actor="builder")

    collab.command_daemon_once(root)
    with pytest.raises(SystemExit, match="daemon once does not satisfy hard mode"):
        collab.command_handoff(
            root,
            task_id="TASK-001",
            actor="builder",
            to_role="architect",
            summary="Ready",
            branch="feat/daemon-ok",
            pr_url=None,
            commit=None,
            skip_verify=False,
            allow_unsynced=False,
        )

    daemon = collab.load_daemon_status(root)
    assert daemon is not None
    assert daemon["status"] == "healthy"
    assert daemon["run_mode"] == "oneshot"


def test_foreground_daemon_without_lock_still_blocks_hard_mode(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stub_git_context(monkeypatch, branch="feat/daemon-lock", head="face123")

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="hard",
        install_hooks=False,
    )
    create_task(root, branch="feat/daemon-lock")
    collab.command_claim(root, task_id="TASK-001", actor="builder")
    collab.daemon_sync_cycle(
        root,
        actor="builder",
        role="builder",
        run_mode="foreground",
    )

    with pytest.raises(SystemExit, match="daemon pid .* is not running"):
        collab.command_handoff(
            root,
            task_id="TASK-001",
            actor="builder",
            to_role="architect",
            summary="Ready",
            branch="feat/daemon-lock",
            pr_url=None,
            commit=None,
            skip_verify=False,
            allow_unsynced=False,
        )


def test_foreground_daemon_allows_hard_mode_handoff(tmp_path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    stub_git_context(monkeypatch, branch="feat/live-daemon", head="feed123")

    collab.command_init(root)
    collab.command_enable(
        root,
        actor="builder",
        role="builder",
        peer="architect",
        enforcement_mode="hard",
        install_hooks=False,
    )
    create_task(root, branch="feat/live-daemon")
    collab.command_claim(root, task_id="TASK-001", actor="builder")
    result = collab.daemon_sync_cycle(
        root,
        actor="builder",
        role="builder",
        run_mode="foreground",
    )
    daemon = result["daemon"]
    monkeypatch.setattr(collab, "daemon_lock_is_held", lambda _root, role: role == "builder")
    monkeypatch.setattr(collab, "daemon_pid_is_alive", lambda pid: pid == daemon["pid"])

    result = collab.command_handoff(
        root,
        task_id="TASK-001",
        actor="builder",
        to_role="architect",
        summary="Ready",
        branch="feat/live-daemon",
        pr_url=None,
        commit=None,
        skip_verify=False,
        allow_unsynced=False,
    )

    assert result == 0

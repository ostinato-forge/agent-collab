#!/usr/bin/env python3
"""Repo-local async collaboration CLI for architect/builder agent workflows."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


ROLES = ("architect", "builder")
TASK_STATUSES = (
    "proposed",
    "in_progress",
    "in_review",
    "changes_requested",
    "approved",
    "blocked",
    "done",
)
REVIEW_DECISIONS = ("approve", "changes_requested", "blocked")
ENFORCEMENT_MODES = ("off", "warn", "hard")
SYNC_SOURCES = ("manual", "command_auto", "hook", "daemon")
HOOK_EVENTS = ("post-checkout", "post-commit", "post-merge", "post-rewrite", "pre-push")
HOOK_MARKER = "# agent-collab managed hook"
STATE_VERSION = 4
TASK_SCHEMA_VERSION = 1
STALE_THRESHOLD_SECONDS = 86_400
PEER_STALE_THRESHOLD_SECONDS = 180
DAEMON_INTERVAL_SECONDS = 15
DAEMON_STALE_THRESHOLD_SECONDS = 90

TASK_STATE_MACHINE = {
    "proposed": {
        "allowed_next": {"in_progress"},
        "required_fields": ("assignee_role", "branch"),
    },
    "in_progress": {
        "allowed_next": {"in_review", "blocked"},
        "required_fields": ("assignee_role",),
    },
    "in_review": {
        "allowed_next": {"approved", "changes_requested", "blocked"},
        "required_fields": ("handoff.summary",),
    },
    "approved": {
        "allowed_next": {"done"},
        "required_fields": ("review.decision",),
    },
    "changes_requested": {
        "allowed_next": {"in_progress", "blocked"},
        "required_fields": ("review.summary",),
    },
    "blocked": {
        "allowed_next": {"in_progress"},
        "required_fields": ("review.summary",),
    },
    "done": {
        "allowed_next": set(),
        "required_fields": (),
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str, *, fallback: str = "task") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def repo_root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def canonical_task_id(task_id: str | None) -> str | None:
    if task_id is None:
        return None
    if re.fullmatch(r"(?i)task-\d+", task_id):
        return task_id.upper()
    return task_id


def collab_dir(root: Path) -> Path:
    return root / ".collab"


def runtime_dir(root: Path) -> Path:
    return collab_dir(root) / "runtime"


def lock_path(root: Path) -> Path:
    return runtime_dir(root) / "mutation.lock"


def daemon_lock_path(root: Path, role: str) -> Path:
    return runtime_dir(root) / f"{role}.daemon.lock"


def active_role_path(root: Path) -> Path:
    return runtime_dir(root) / "active-role.txt"


def role_runtime_path(root: Path, role: str) -> Path:
    return runtime_dir(root) / f"{role}.json"


def legacy_agent_config_path(root: Path) -> Path:
    return runtime_dir(root) / "agent.json"


def legacy_daemon_status_path(root: Path) -> Path:
    return runtime_dir(root) / "daemon.json"


def hooks_manifest_path(root: Path) -> Path:
    return runtime_dir(root) / "hooks.json"


def hooks_backup_dir(root: Path) -> Path:
    return runtime_dir(root) / "hook-backups"


def state_path(root: Path) -> Path:
    return collab_dir(root) / "state.json"


def tasks_dir(root: Path) -> Path:
    return collab_dir(root) / "tasks"


def inbox_path(root: Path, role: str) -> Path:
    return collab_dir(root) / "inbox" / f"{role}.md"


def reviews_dir(root: Path) -> Path:
    return collab_dir(root) / "reviews"


def default_role_state() -> dict[str, Any]:
    return {
        "current_task_id": None,
        "queue_depth": 0,
        "last_sync_at": None,
        "last_sync_by": None,
        "last_sync_task_id": None,
        "last_sync_branch": None,
        "last_sync_head": None,
        "last_sync_source": None,
        "last_sync_reason": None,
        "last_sync_note": None,
        "last_inbox_mtime": 0.0,
    }


def actor_role(actor: str) -> str | None:
    return actor if actor in ROLES else None


def default_state(project_name: str) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "project": project_name,
        "protocol": "async_git_pr_v1",
        "updated_at": utc_now(),
        "last_task_number": 0,
        "summary": {
            "total": 0,
            "queued_for_architect": 0,
            "queued_for_builder": 0,
            "proposed": 0,
            "in_progress": 0,
            "in_review": 0,
            "changes_requested": 0,
            "approved": 0,
            "blocked": 0,
            "done": 0,
        },
        "roles": {
            "architect": default_role_state(),
            "builder": default_role_state(),
        },
    }


def atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return read_json(path)


def default_daemon_status() -> dict[str, Any]:
    return {
        "pid": None,
        "run_mode": None,
        "started_at": None,
        "last_heartbeat_at": None,
        "last_observed_branch": None,
        "last_observed_head": None,
        "status": "never_started",
    }


def default_agent_config(
    root: Path,
    *,
    actor: str,
    role: str,
    peer: str,
    enforcement_mode: str,
) -> dict[str, Any]:
    return {
        "actor": actor,
        "role": role,
        "peer": peer,
        "enforcement_mode": enforcement_mode,
        "installed_at": utc_now(),
        "repo_root": str(root),
        "daemon": default_daemon_status(),
    }


def normalize_agent_config(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(payload)
    daemon = default_daemon_status()
    daemon.update(normalized.get("daemon") or {})
    normalized["daemon"] = daemon
    return normalized


def configured_runtime_roles(root: Path) -> list[str]:
    roles: list[str] = []
    for role in ROLES:
        if role_runtime_path(root, role).exists():
            roles.append(role)
            continue
        legacy = read_optional_json(legacy_agent_config_path(root))
        if legacy is not None and legacy.get("role") == role:
            roles.append(role)
    return roles


def load_active_runtime_role(root: Path) -> str | None:
    path = active_role_path(root)
    if not path.exists():
        return None
    role = path.read_text(encoding="utf-8").strip()
    return role if role in ROLES else None


def set_active_runtime_role(root: Path, role: str | None) -> None:
    path = active_role_path(root)
    if role is None:
        if path.exists():
            path.unlink()
        return
    atomic_write_text(path, role + "\n")


def resolve_runtime_role(root: Path, role: str | None = None) -> str | None:
    if role is not None:
        return role
    active_role = load_active_runtime_role(root)
    if active_role is not None:
        return active_role
    hooks_manifest = load_hooks_manifest(root) or {}
    hook_role = hooks_manifest.get("active_role")
    if hook_role in ROLES:
        return hook_role
    configured = configured_runtime_roles(root)
    if len(configured) == 1:
        return configured[0]
    if not configured:
        return None
    raise SystemExit("multiple runtime roles configured; pass --role")


def agent_config_path(root: Path, role: str | None = None) -> Path:
    resolved_role = resolve_runtime_role(root, role)
    if resolved_role is None:
        return legacy_agent_config_path(root)
    return role_runtime_path(root, resolved_role)


def daemon_status_path(root: Path, role: str | None = None) -> Path:
    resolved_role = resolve_runtime_role(root, role)
    if resolved_role is None:
        return legacy_daemon_status_path(root)
    return role_runtime_path(root, resolved_role)


def load_agent_config(root: Path, role: str | None = None) -> dict[str, Any] | None:
    resolved_role = resolve_runtime_role(root, role)
    if resolved_role is None:
        return None
    path = role_runtime_path(root, resolved_role)
    payload = read_optional_json(path)
    if payload is None:
        legacy_agent = read_optional_json(legacy_agent_config_path(root))
        if legacy_agent is None or legacy_agent.get("role") != resolved_role:
            return None
        payload = normalize_agent_config(legacy_agent)
        legacy_daemon = read_optional_json(legacy_daemon_status_path(root))
        if legacy_daemon is not None:
            payload["daemon"].update(legacy_daemon)
        atomic_write_json(path, payload)
    return normalize_agent_config(payload)


def write_agent_config(
    root: Path,
    *,
    actor: str,
    role: str,
    peer: str,
    enforcement_mode: str,
) -> dict[str, Any]:
    config = default_agent_config(
        root,
        actor=actor,
        role=role,
        peer=peer,
        enforcement_mode=enforcement_mode,
    )
    existing = load_agent_config(root, role=role)
    if existing is not None:
        config["daemon"] = normalize_agent_config(existing)["daemon"]
    atomic_write_json(role_runtime_path(root, role), config)
    set_active_runtime_role(root, role)
    return config


def load_daemon_status(root: Path, role: str | None = None) -> dict[str, Any] | None:
    config = load_agent_config(root, role=role)
    if config is None:
        return None
    return normalize_agent_config(config)["daemon"]


def write_daemon_status(
    root: Path,
    *,
    role: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    config = load_agent_config(root, role=role)
    if config is None:
        raise SystemExit(f"agent runtime is not enabled for role {role}")
    config = normalize_agent_config(config)
    daemon = default_daemon_status()
    daemon.update(payload)
    config["daemon"] = daemon
    atomic_write_json(role_runtime_path(root, role), config)
    return daemon


def load_hooks_manifest(root: Path) -> dict[str, Any] | None:
    return read_optional_json(hooks_manifest_path(root))


def write_hooks_manifest(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    atomic_write_json(hooks_manifest_path(root), payload)
    return payload


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def current_inbox_mtime(root: Path, role: str) -> float:
    inbox = inbox_path(root, role)
    return inbox.stat().st_mtime if inbox.exists() else 0.0


def ensure_initialized(root: Path) -> None:
    collab = collab_dir(root)
    (collab / "inbox").mkdir(parents=True, exist_ok=True)
    (collab / "tasks").mkdir(parents=True, exist_ok=True)
    (collab / "reviews").mkdir(parents=True, exist_ok=True)
    runtime_dir(root).mkdir(parents=True, exist_ok=True)

    defaults: dict[Path, str] = {
        collab / "README.md": (
            "# `.collab`\n\n"
            "Repo-local coordination bus for autonomous multi-agent work.\n"
        ),
        collab / "inbox" / "architect.md": "# Architect Inbox\n\n",
        collab / "inbox" / "builder.md": "# Builder Inbox\n\n",
        collab / "tasks" / "README.md": "# Tasks\n\n",
        collab / "reviews" / "README.md": "# Reviews\n\n",
    }
    for path, contents in defaults.items():
        if not path.exists():
            path.write_text(contents, encoding="utf-8")

    if not state_path(root).exists():
        atomic_write_json(state_path(root), default_state(root.name))


class _LockState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.depth = 0
        self.handle: Any | None = None


_LOCK_STATES: dict[str, _LockState] = {}
_LOCK_STATES_GUARD = threading.Lock()


def _lock_state_for(root: Path) -> _LockState:
    key = str(root)
    with _LOCK_STATES_GUARD:
        state = _LOCK_STATES.get(key)
        if state is None:
            state = _LockState()
            _LOCK_STATES[key] = state
        return state


def _acquire_platform_lock(handle: Any) -> None:
    if os.name == "nt":
        if msvcrt is None:  # pragma: no cover - Windows import guard
            raise SystemExit("Windows file locking support unavailable")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    if fcntl is None:  # pragma: no cover - POSIX import guard
        raise SystemExit("POSIX file locking support unavailable")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _try_acquire_platform_lock(handle: Any) -> bool:
    if os.name == "nt":
        if msvcrt is None:  # pragma: no cover - Windows import guard
            raise SystemExit("Windows file locking support unavailable")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    if fcntl is None:  # pragma: no cover - POSIX import guard
        raise SystemExit("POSIX file locking support unavailable")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _release_platform_lock(handle: Any) -> None:
    if os.name == "nt":
        if msvcrt is None:  # pragma: no cover - Windows import guard
            return
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def collab_lock(root: Path):
    ensure_initialized(root)
    state = _lock_state_for(root)
    state.lock.acquire()
    try:
        if state.depth == 0:
            handle = lock_path(root).open("a+b")
            _acquire_platform_lock(handle)
            state.handle = handle
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0 and state.handle is not None:
                handle = state.handle
                state.handle = None
                try:
                    _release_platform_lock(handle)
                finally:
                    handle.close()
    finally:
        state.lock.release()


def daemon_lock_is_held(root: Path, role: str) -> bool:
    ensure_initialized(root)
    handle = daemon_lock_path(root, role).open("a+b")
    try:
        if not _try_acquire_platform_lock(handle):
            return True
        _release_platform_lock(handle)
        return False
    finally:
        handle.close()


def load_state(root: Path) -> dict[str, Any]:
    ensure_initialized(root)
    return read_json(state_path(root))


def task_file(root: Path, task_id: str) -> Path:
    return tasks_dir(root) / f"{canonical_task_id(task_id)}.json"


def migrate_task(task: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    migrated = copy.deepcopy(task)
    changed = False
    if migrated.get("schema_version") != TASK_SCHEMA_VERSION:
        migrated["schema_version"] = TASK_SCHEMA_VERSION
        changed = True
    return migrated, changed


def normalize_task(task: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    normalized, changed = migrate_task(task)

    defaults: dict[str, Any] = {
        "source_plan": None,
        "branch": None,
        "files": [],
        "acceptance": [],
        "note": None,
        "handoff": {},
        "review": {},
        "history": [],
        "verification_cmd": None,
        "verification": {},
        "last_activity_at": normalized.get("updated_at") or normalized.get("created_at"),
        "stale_flagged_at": None,
        "stale_reason": None,
        "depends_on": [],
    }

    for field, default in defaults.items():
        if field not in normalized or normalized[field] is None:
            normalized[field] = copy.deepcopy(default)
            changed = True

    if isinstance(normalized.get("verification_cmd"), str):
        normalized["verification_cmd"] = shlex.split(normalized["verification_cmd"])
        changed = True

    if normalized.get("verification_cmd") == []:
        normalized["verification_cmd"] = None
        changed = True

    return normalized, changed


def list_task_paths(root: Path) -> list[Path]:
    return sorted(
        path
        for path in tasks_dir(root).glob("TASK-*.json")
        if path.is_file()
    )


def load_task_payload(path: Path) -> dict[str, Any]:
    task = read_json(path)
    normalized, changed = normalize_task(task)
    if changed:
        atomic_write_json(path, normalized)
    return normalized


def load_tasks(root: Path) -> list[dict[str, Any]]:
    ensure_initialized(root)
    return [load_task_payload(path) for path in list_task_paths(root)]


def load_task(root: Path, task_id: str) -> dict[str, Any]:
    path = task_file(root, task_id)
    if not path.exists():
        raise SystemExit(f"task not found: {task_id}")
    return load_task_payload(path)


def touch_task_activity(task: dict[str, Any], *, when: str) -> None:
    task["last_activity_at"] = when
    task["stale_flagged_at"] = None
    task["stale_reason"] = None


def save_task(root: Path, task: dict[str, Any], *, touch_activity: bool = False) -> None:
    now = utc_now()
    if touch_activity:
        touch_task_activity(task, when=now)
    task["updated_at"] = now
    atomic_write_json(task_file(root, task["task_id"]), task)


def append_history(task: dict[str, Any], *, actor: str, event: str, detail: str) -> None:
    task.setdefault("history", []).append(
        {
            "at": utc_now(),
            "actor": actor,
            "event": event,
            "detail": detail,
        }
    )


def summary_for_tasks(tasks: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "total": len(tasks),
        "queued_for_architect": 0,
        "queued_for_builder": 0,
        "proposed": 0,
        "in_progress": 0,
        "in_review": 0,
        "changes_requested": 0,
        "approved": 0,
        "blocked": 0,
        "done": 0,
    }
    for task in tasks:
        status = task["status"]
        summary[status] += 1
        assignee_role = task.get("assignee_role")
        if assignee_role == "architect" and status != "done":
            summary["queued_for_architect"] += 1
        if assignee_role == "builder" and status != "done":
            summary["queued_for_builder"] += 1
    return summary


def role_priority(role: str, task: dict[str, Any]) -> tuple[int, str, str]:
    status = task["status"]
    if role == "builder":
        priorities = {
            "changes_requested": 0,
            "in_progress": 1,
            "approved": 2,
            "proposed": 3,
            "blocked": 4,
            "in_review": 5,
            "done": 6,
        }
    else:
        priorities = {
            "in_review": 0,
            "blocked": 1,
            "proposed": 2,
            "changes_requested": 3,
            "in_progress": 4,
            "approved": 5,
            "done": 6,
        }
    return (
        priorities.get(status, 9),
        task.get("updated_at", ""),
        task["task_id"],
    )


def queued_tasks(tasks: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return sorted(
        [
            task
            for task in tasks
            if task.get("assignee_role") == role and task.get("status") != "done"
        ],
        key=lambda item: role_priority(role, item),
    )


def tasks_by_id(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {task["task_id"]: task for task in tasks}


def nested_value(payload: dict[str, Any], dotted_path: str) -> Any:
    current: Any = payload
    for segment in dotted_path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def missing_required_field(value: Any) -> bool:
    return value in (None, "", [], {})


def validate_transition(task: dict[str, Any], new_status: str) -> None:
    current_status = task["status"]
    allowed = TASK_STATE_MACHINE.get(current_status, {}).get("allowed_next", set())
    if new_status not in allowed:
        raise SystemExit(
            f"invalid transition for {task['task_id']}: {current_status} -> {new_status}"
        )


def validate_required_fields(task: dict[str, Any], target_status: str) -> None:
    requirements = TASK_STATE_MACHINE.get(target_status, {}).get("required_fields", ())
    missing = [
        field
        for field in requirements
        if missing_required_field(nested_value(task, field))
    ]
    if missing:
        missing_list = ", ".join(missing)
        raise SystemExit(f"{task['task_id']} missing required fields for {target_status}: {missing_list}")


def verification_status(task: dict[str, Any]) -> str | None:
    verification = task.get("verification") or {}
    if not verification:
        return None
    if verification.get("skipped"):
        return "skipped"
    return "passed" if verification.get("passed") else "failed"


def task_context_lines(
    task: dict[str, Any],
    *,
    indent: str = "  ",
    include_files: bool = False,
    include_acceptance: bool = False,
) -> list[str]:
    lines: list[str] = []
    if task.get("branch"):
        lines.append(f"{indent}branch: {task['branch']}")
    if task.get("source_plan"):
        lines.append(f"{indent}source_plan: {task['source_plan']}")

    verification = task.get("verification") or {}
    if verification:
        status = verification_status(task)
        lines.append(f"{indent}verification_status: {status}")
        if verification.get("ran_at"):
            lines.append(f"{indent}verification_ran_at: {verification['ran_at']}")
        if verification.get("branch"):
            lines.append(f"{indent}verification_branch: {verification['branch']}")
        if verification.get("head"):
            lines.append(f"{indent}verification_head: {verification['head']}")

    handoff = task.get("handoff") or {}
    if handoff:
        if handoff.get("at"):
            lines.append(f"{indent}handoff_at: {handoff['at']}")
        if handoff.get("by"):
            lines.append(f"{indent}handoff_by: {handoff['by']}")
        if handoff.get("branch"):
            lines.append(f"{indent}handoff_branch: {handoff['branch']}")
        if handoff.get("pr_url"):
            lines.append(f"{indent}handoff_pr_url: {handoff['pr_url']}")
        if handoff.get("commit"):
            lines.append(f"{indent}handoff_commit: {handoff['commit']}")
        if handoff.get("summary"):
            lines.append(f"{indent}handoff_summary: {handoff['summary']}")
        handoff_verification = handoff.get("verification") or {}
        if handoff_verification.get("status"):
            lines.append(f"{indent}handoff_verification: {handoff_verification['status']}")

    review = task.get("review") or {}
    if review:
        if review.get("at"):
            lines.append(f"{indent}review_at: {review['at']}")
        if review.get("by"):
            lines.append(f"{indent}review_by: {review['by']}")
        if review.get("decision"):
            lines.append(f"{indent}review_decision: {review['decision']}")
        if review.get("summary"):
            lines.append(f"{indent}review_summary: {review['summary']}")

    if task.get("stale_flagged_at"):
        lines.append(f"{indent}stale_flagged_at: {task['stale_flagged_at']}")
    if task.get("stale_reason"):
        lines.append(f"{indent}stale_reason: {task['stale_reason']}")

    if include_files:
        for path in task.get("files", []):
            lines.append(f"{indent}file: {path}")
    if include_acceptance:
        for item in task.get("acceptance", []):
            lines.append(f"{indent}accept: {item}")
    if task.get("note"):
        lines.append(f"{indent}note: {task['note']}")
    return lines


def build_refreshed_state(
    root: Path,
    *,
    state: dict[str, Any] | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ensure_initialized(root)
    current = copy.deepcopy(state) if state is not None else load_state(root)
    current_tasks = tasks if tasks is not None else load_tasks(root)
    current["version"] = STATE_VERSION
    current["project"] = root.name
    current["summary"] = summary_for_tasks(current_tasks)
    current["updated_at"] = utc_now()
    existing_roles = current.get("roles", {})
    refreshed_roles: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        role_tasks = queued_tasks(current_tasks, role)
        role_state = default_role_state()
        role_state.update(existing_roles.get(role, {}))
        role_state["current_task_id"] = role_tasks[0]["task_id"] if role_tasks else None
        role_state["queue_depth"] = len(role_tasks)
        refreshed_roles[role] = role_state
    current["roles"] = refreshed_roles
    return current


def refresh_state(root: Path, *, persist: bool = True) -> dict[str, Any]:
    state = build_refreshed_state(root)
    if persist:
        atomic_write_json(state_path(root), state)
    return state


def resolve_checkpoint_branch(
    tasks: list[dict[str, Any]],
    *,
    task_id: str | None,
    branch: str | None,
) -> str | None:
    task_id = canonical_task_id(task_id)
    if branch is not None:
        return branch
    if task_id is None:
        return None
    task = tasks_by_id(tasks).get(task_id)
    if task is None:
        return None
    handoff = task.get("handoff") or {}
    return handoff.get("branch") or task.get("branch")


def resolve_sync_context(
    root: Path,
    tasks: list[dict[str, Any]],
    *,
    task_id: str | None,
    branch: str | None,
    head: str | None,
) -> tuple[str | None, str | None]:
    context = git_context(root)
    resolved_branch = branch or context.get("branch") or resolve_checkpoint_branch(
        tasks,
        task_id=task_id,
        branch=None,
    )
    resolved_head = head or context.get("head")
    return resolved_branch, resolved_head


def update_role_sync(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    root: Path,
    role: str,
    actor: str,
    task_id: str | None,
    branch: str | None,
    head: str | None,
    note: str | None,
    inbox_mtime: float,
    sync_source: str,
    sync_reason: str | None,
) -> dict[str, Any]:
    role_state = default_role_state()
    role_state.update(state["roles"].get(role, {}))
    checkpoint_task_id = canonical_task_id(task_id) or canonical_task_id(
        role_state.get("current_task_id")
    )
    checkpoint_branch, checkpoint_head = resolve_sync_context(
        root,
        tasks,
        task_id=checkpoint_task_id,
        branch=branch,
        head=head,
    )
    role_state["last_sync_at"] = utc_now()
    role_state["last_sync_by"] = actor
    role_state["last_sync_task_id"] = checkpoint_task_id
    role_state["last_sync_branch"] = checkpoint_branch
    role_state["last_sync_head"] = checkpoint_head
    role_state["last_sync_source"] = sync_source
    role_state["last_sync_reason"] = sync_reason or note
    role_state["last_sync_note"] = note
    role_state["last_inbox_mtime"] = float(inbox_mtime)
    state["roles"][role] = role_state
    state["updated_at"] = utc_now()
    return state


def auto_sync_actor_state(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    root: Path,
    actor: str,
    task_id: str | None,
    branch: str | None,
    head: str | None,
    note: str,
    sync_source: str = "command_auto",
    sync_reason: str | None = None,
) -> dict[str, Any]:
    role = actor_role(actor)
    if role is None:
        return state
    return update_role_sync(
        state,
        tasks,
        root=root,
        role=role,
        actor=actor,
        task_id=task_id,
        branch=branch,
        head=head,
        note=note,
        inbox_mtime=current_inbox_mtime(root, role),
        sync_source=sync_source,
        sync_reason=sync_reason,
    )


def record_role_sync(
    root: Path,
    *,
    role: str,
    actor: str,
    task_id: str | None,
    branch: str | None,
    head: str | None,
    note: str | None,
    inbox_mtime: float,
    sync_source: str = "manual",
    sync_reason: str | None = None,
) -> dict[str, Any]:
    with collab_lock(root):
        tasks = load_tasks(root)
        state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
        state = update_role_sync(
            state,
            tasks,
            root=root,
            role=role,
            actor=actor,
            task_id=task_id,
            branch=branch,
            head=head,
            note=note,
            inbox_mtime=inbox_mtime,
            sync_source=sync_source,
            sync_reason=sync_reason,
        )
        atomic_write_json(state_path(root), state)
        return state


def task_updates_since(
    tasks: list[dict[str, Any]],
    *,
    role: str,
    since: datetime | None,
) -> list[dict[str, Any]]:
    queue = queued_tasks(tasks, role)
    if since is None:
        return queue
    updated: list[dict[str, Any]] = []
    for task in queue:
        updated_at = parse_timestamp(task.get("updated_at"))
        if updated_at is not None and updated_at > since:
            updated.append(task)
    return updated


def role_sync_age(role_state: dict[str, Any]) -> int | None:
    synced_at = parse_timestamp(role_state.get("last_sync_at"))
    if synced_at is None:
        return None
    return int((datetime.now(timezone.utc) - synced_at).total_seconds())


def peer_is_stale(state: dict[str, Any], *, role: str, max_age_seconds: int) -> tuple[str, bool, int | None]:
    peer_role = next(item for item in ROLES if item != role)
    peer_state = default_role_state()
    peer_state.update(state["roles"].get(peer_role, {}))
    age_seconds = role_sync_age(peer_state)
    if age_seconds is None:
        return peer_role, False, None
    return peer_role, age_seconds > max_age_seconds, age_seconds


def peer_sync_summary(
    state: dict[str, Any],
    *,
    role: str,
    max_age_seconds: int,
) -> tuple[str, str]:
    peer_role = next(item for item in ROLES if item != role)
    peer_state = default_role_state()
    peer_state.update(state["roles"].get(peer_role, {}))
    age_seconds = role_sync_age(peer_state)
    if age_seconds is None:
        return peer_role, "never synced"

    freshness = "STALE" if age_seconds > max_age_seconds else "fresh"
    summary = (
        f"last_sync_at={peer_state.get('last_sync_at')} "
        f"last_sync_by={peer_state.get('last_sync_by')} "
        f"age_seconds={age_seconds} status={freshness}"
    )
    return peer_role, summary


def append_inbox_entry(
    root: Path,
    *,
    to_role: str,
    from_actor: str,
    subject: str,
    body: str,
) -> None:
    ensure_initialized(root)
    entry = (
        f"\n## {utc_now()} | from {from_actor}\n\n"
        f"Subject: {subject}\n\n"
        f"{body}\n"
    )
    with inbox_path(root, to_role).open("a", encoding="utf-8") as handle:
        handle.write(entry)


def task_is_stale(
    task: dict[str, Any],
    *,
    now: datetime,
    threshold_seconds: int,
) -> bool:
    if task.get("status") not in {"in_progress", "in_review"}:
        return False
    activity_at = parse_timestamp(task.get("last_activity_at") or task.get("updated_at"))
    if activity_at is None:
        return False
    age_seconds = int((now - activity_at).total_seconds())
    return age_seconds > threshold_seconds


def check_stale_tasks(
    root: Path,
    *,
    actor: str,
    threshold_seconds: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with collab_lock(root):
        tasks = load_tasks(root)
        stale_tasks: list[dict[str, Any]] = []
        newly_flagged: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for task in tasks:
            if not task_is_stale(task, now=now, threshold_seconds=threshold_seconds):
                continue
            stale_tasks.append(task)
            if task.get("stale_flagged_at"):
                continue

            reason = (
                f"no activity for more than {threshold_seconds}s while task is {task['status']}"
            )
            task["stale_flagged_at"] = utc_now()
            task["stale_reason"] = reason
            append_history(task, actor=actor, event="stale", detail=reason)
            save_task(root, task)
            newly_flagged.append(task)

        if newly_flagged:
            lines = [
                f"- {task['task_id']} [{task['status']}] {task['title']}"
                for task in newly_flagged
            ]
            append_inbox_entry(
                root,
                to_role="architect",
                from_actor=actor,
                subject="Stale tasks detected",
                body="\n".join(lines),
            )
            state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
            atomic_write_json(state_path(root), state)

        return stale_tasks, newly_flagged


def git_context(root: Path) -> dict[str, Any]:
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return {
            "branch": branch,
            "head": head,
            "error": None,
        }
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        return {
            "branch": None,
            "head": None,
            "error": detail or "git context unavailable",
        }


def git_path(root: Path, relative: str) -> Path | None:
    try:
        resolved = subprocess.run(
            ["git", "rev-parse", "--git-path", relative],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return Path(resolved if os.path.isabs(resolved) else root / resolved)


def git_hooks_dir(root: Path) -> Path | None:
    return git_path(root, "hooks")


def default_peer(role: str) -> str:
    return next(item for item in ROLES if item != role)


def hook_backup_path(root: Path, hook_name: str) -> Path:
    return hooks_backup_dir(root) / f"{hook_name}.orig"


def hook_script_payload(
    *,
    root: Path,
    role: str | None,
    hook_name: str,
    backup_path: Path | None,
) -> str:
    python_path = Path(sys.executable).resolve().as_posix()
    script_path = Path(__file__).resolve().as_posix()
    root_path = root.resolve().as_posix()
    lines = [
        "#!/bin/sh",
        HOOK_MARKER,
        f"# hook: {hook_name}",
        "set -eu",
    ]
    if backup_path is not None:
        backup = backup_path.resolve().as_posix()
        lines.extend(
            [
                f'if [ -f "{backup}" ]; then',
                f'  if [ -x "{backup}" ]; then',
                f'    "{backup}" "$@"',
                "  else",
                f'    sh "{backup}" "$@"',
                "  fi",
                "fi",
            ]
        )
    role_arg = f' --role "{role}"' if role is not None else ""
    lines.append(
        f'"{python_path}" "{script_path}" --root "{root_path}" git-event{role_arg} --event "{hook_name}" "$@"'
    )
    lines.append("")
    return "\n".join(lines)


def hook_manifest_entry(
    *,
    hook_name: str,
    hook_path: Path,
    backup_path: Path | None,
) -> dict[str, Any]:
    return {
        "hook_path": str(hook_path),
        "backup_path": str(backup_path) if backup_path is not None else None,
        "managed": True,
    }


def install_git_hooks(root: Path, *, role: str | None = None) -> dict[str, Any]:
    hooks_dir = git_hooks_dir(root)
    if hooks_dir is None:
        raise SystemExit("git hooks path unavailable; is this a git worktree?")

    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_backup_dir(root).mkdir(parents=True, exist_ok=True)

    manifest = load_hooks_manifest(root) or {"installed_at": utc_now(), "hooks": {}}
    previous_hooks = manifest.get("hooks", {})
    installed: dict[str, Any] = {}

    for hook_name in HOOK_EVENTS:
        hook_path = hooks_dir / hook_name
        previous_entry = previous_hooks.get(hook_name, {})
        backup_path = (
            Path(previous_entry["backup_path"])
            if previous_entry.get("backup_path")
            else None
        )
        if hook_path.exists():
            current_contents = hook_path.read_text(encoding="utf-8")
            if HOOK_MARKER not in current_contents:
                backup_path = hook_backup_path(root, hook_name)
                atomic_write_text(backup_path, current_contents)
        payload = hook_script_payload(
            root=root,
            role=role,
            hook_name=hook_name,
            backup_path=backup_path if backup_path is not None and backup_path.exists() else None,
        )
        atomic_write_text(hook_path, payload)
        try:
            hook_path.chmod(0o755)
        except OSError:
            pass
        installed[hook_name] = hook_manifest_entry(
            hook_name=hook_name,
            hook_path=hook_path,
            backup_path=backup_path if backup_path is not None and backup_path.exists() else None,
        )

    manifest["installed_at"] = utc_now()
    manifest["hooks"] = installed
    manifest["active_role"] = role
    write_hooks_manifest(root, manifest)
    if role is not None:
        set_active_runtime_role(root, role)
    return manifest


def uninstall_git_hooks(root: Path) -> dict[str, Any]:
    manifest = load_hooks_manifest(root) or {"hooks": {}}
    hook_entries = manifest.get("hooks", {})
    for hook_name, entry in hook_entries.items():
        hook_path = Path(entry.get("hook_path", ""))
        backup_path = Path(entry["backup_path"]) if entry.get("backup_path") else None
        if backup_path is not None and backup_path.exists():
            atomic_write_text(hook_path, backup_path.read_text(encoding="utf-8"))
            try:
                hook_path.chmod(0o755)
            except OSError:
                pass
        elif hook_path.exists():
            contents = hook_path.read_text(encoding="utf-8")
            if HOOK_MARKER in contents:
                hook_path.unlink()
    if hooks_manifest_path(root).exists():
        hooks_manifest_path(root).unlink()
    return manifest


def daemon_status_age(payload: dict[str, Any] | None) -> int | None:
    if not payload:
        return None
    heartbeat = parse_timestamp(payload.get("last_heartbeat_at"))
    if heartbeat is None:
        return None
    return int((datetime.now(timezone.utc) - heartbeat).total_seconds())


def daemon_pid_is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def daemon_health(
    payload: dict[str, Any] | None,
    *,
    root: Path | None = None,
    role: str | None = None,
    max_age_seconds: int = DAEMON_STALE_THRESHOLD_SECONDS,
) -> tuple[str, int | None]:
    if not payload:
        return "missing", None
    age = daemon_status_age(payload)
    status = payload.get("status")
    pid = payload.get("pid")
    run_mode = payload.get("run_mode")
    if status in (None, "never_started") and pid is None and age is None:
        return "missing", None
    if status == "stopped":
        return "stopped", age
    if run_mode == "oneshot":
        return "oneshot", age
    if root is not None and role is not None and not daemon_lock_is_held(root, role):
        return "dead", age
    if pid is None:
        return "missing", age
    if not daemon_pid_is_alive(pid):
        return "dead", age
    if age is None:
        return "missing", None
    if age > max_age_seconds:
        return "stale", age
    return "fresh", age


def daemon_is_fresh(payload: dict[str, Any] | None, *, max_age_seconds: int = DAEMON_STALE_THRESHOLD_SECONDS) -> bool:
    health, _ = daemon_health(payload=payload, max_age_seconds=max_age_seconds)
    return health == "fresh"


def load_role_state(root: Path, role: str) -> dict[str, Any]:
    state = build_refreshed_state(root, state=load_state(root), tasks=load_tasks(root))
    role_state = default_role_state()
    role_state.update(state["roles"].get(role, {}))
    return role_state


def daemon_issue_for_health(health: str, payload: dict[str, Any] | None) -> str:
    if health == "missing":
        return "daemon heartbeat is missing"
    if health == "stopped":
        return "daemon is stopped"
    if health == "oneshot":
        return "daemon once does not satisfy hard mode; run daemon up"
    if health == "dead":
        pid = payload.get("pid") if payload else None
        return f"daemon pid {pid} is not running"
    if health == "stale":
        return "daemon heartbeat is stale"
    return "daemon state is unhealthy"


def sync_readiness(
    root: Path,
    *,
    actor: str,
    role: str,
    context: str,
) -> dict[str, Any]:
    agent = load_agent_config(root, role=role)
    mode = (agent or {}).get("enforcement_mode", "off")
    verdict = {
        "ok": True,
        "blocked": False,
        "mode": mode,
        "issues": [],
        "context": context,
    }
    if mode == "off":
        return verdict

    git = git_context(root)
    role_state = load_role_state(root, role)
    daemon = load_daemon_status(root, role=role)

    issues: list[str] = []
    if agent is None:
        issues.append("agent runtime is not enabled")
    if git.get("error"):
        issues.append(f"git context unavailable: {git['error']}")

    current_branch = git.get("branch")
    current_head = git.get("head")
    last_branch = role_state.get("last_sync_branch")
    last_head = role_state.get("last_sync_head")

    if current_branch and last_branch and current_branch != last_branch:
        issues.append(f"current branch {current_branch} differs from last sync {last_branch}")
    if current_head and last_head and current_head != last_head:
        issues.append(f"current HEAD {current_head} differs from last sync {last_head}")

    task_id = canonical_task_id(role_state.get("current_task_id"))
    if task_id:
        task = tasks_by_id(load_tasks(root)).get(task_id)
        if task is not None and task.get("branch") and current_branch and task["branch"] != current_branch:
            issues.append(f"current task {task_id} expects branch {task['branch']}, got {current_branch}")

    daemon_health_state, daemon_age = daemon_health(root=root, role=role, payload=daemon)
    if mode != "off" and daemon_health_state != "fresh":
        issues.append(daemon_issue_for_health(daemon_health_state, daemon))

    verdict["issues"] = issues
    verdict["ok"] = not issues
    verdict["blocked"] = bool(issues and mode == "hard")
    verdict["git"] = git
    verdict["role_state"] = role_state
    verdict["daemon"] = daemon
    verdict["daemon_health"] = daemon_health_state
    verdict["daemon_age_seconds"] = daemon_age
    verdict["actor"] = actor
    verdict["role"] = role
    return verdict


def ensure_sync_ready(
    root: Path,
    *,
    actor: str,
    role: str,
    context: str,
    allow_unsynced: bool,
) -> dict[str, Any]:
    readiness = sync_readiness(root, actor=actor, role=role, context=context)
    if readiness["ok"] or allow_unsynced:
        return readiness
    issues = "; ".join(readiness["issues"])
    raise SystemExit(f"{context} blocked: {issues}")


def daemon_sync_cycle(
    root: Path,
    *,
    actor: str,
    role: str,
    note: str | None = None,
    run_mode: str = "foreground",
) -> dict[str, Any]:
    current_task_id = load_role_state(root, role).get("current_task_id")
    state = record_role_sync(
        root,
        role=role,
        actor=actor,
        task_id=current_task_id,
        branch=None,
        head=None,
        note=note or "daemon heartbeat",
        inbox_mtime=current_inbox_mtime(root, role),
        sync_source="daemon",
        sync_reason="daemon poll",
    )
    git = git_context(root)
    existing_daemon = load_daemon_status(root, role=role) or {}
    daemon_payload = {
        "actor": actor,
        "role": role,
        "pid": os.getpid(),
        "run_mode": run_mode,
        "started_at": (
            existing_daemon.get("started_at")
            if existing_daemon.get("pid") == os.getpid()
            and existing_daemon.get("run_mode") == run_mode
            else utc_now()
        ),
        "last_heartbeat_at": utc_now(),
        "last_observed_branch": git.get("branch"),
        "last_observed_head": git.get("head"),
        "status": "healthy" if not git.get("error") else "git-error",
    }
    write_daemon_status(root, role=role, payload=daemon_payload)
    return {"state": state, "daemon": daemon_payload}


def render_command(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def run_verification(root: Path, task: dict[str, Any]) -> dict[str, Any]:
    command = list(task.get("verification_cmd") or [])
    context = git_context(root)
    verification: dict[str, Any] = {
        "passed": False,
        "ran_at": utc_now(),
        "exit_code": None,
        "branch": context.get("branch"),
        "head": context.get("head"),
        "command": command,
        "output": "",
    }

    if not command:
        verification["output"] = "no verification command configured"
        return verification

    if context.get("error"):
        verification["output"] = context["error"]
        return verification

    expected_branch = task.get("branch")
    if expected_branch and context.get("branch") != expected_branch:
        verification["output"] = (
            f"branch mismatch: expected {expected_branch}, got {context.get('branch')}"
        )
        return verification

    completed = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    verification["exit_code"] = completed.returncode
    combined_output = completed.stdout
    if completed.stderr:
        combined_output = f"{combined_output}\n{completed.stderr}" if combined_output else completed.stderr
    verification["output"] = combined_output.strip()
    verification["passed"] = completed.returncode == 0
    return verification


def render_review_markdown(
    *,
    task: dict[str, Any],
    actor: str,
    decision: str,
    summary: str,
    findings: list[str],
) -> str:
    lines = [
        f"# Review {task['task_id']}",
        "",
        f"- Reviewer: {actor}",
        f"- Decision: {decision}",
        f"- Reviewed at: {utc_now()}",
        "",
        "## Summary",
        "",
        summary,
        "",
    ]
    if findings:
        lines.extend(["## Findings", ""])
        lines.extend(f"- {item}" for item in findings)
        lines.append("")
    verification = task.get("verification") or {}
    if verification:
        lines.extend(["## Verification", ""])
        lines.append(f"- status: {verification_status(task)}")
        for key in ("ran_at", "branch", "head", "exit_code"):
            value = verification.get(key)
            if value not in (None, ""):
                lines.append(f"- {key}: {value}")
        if verification.get("command"):
            lines.append(f"- command: {render_command(verification['command'])}")
        lines.append("")
    handoff = task.get("handoff") or {}
    if handoff:
        lines.extend(["## Handoff Context", ""])
        for key in ("branch", "pr_url", "commit", "summary"):
            value = handoff.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        lines.append("")
    return "\n".join(lines)


def command_init(root: Path) -> int:
    with collab_lock(root):
        ensure_initialized(root)
        state = build_refreshed_state(root, state=load_state(root), tasks=load_tasks(root))
        atomic_write_json(state_path(root), state)
    print(f"initialized {collab_dir(root)}")
    return 0


def command_enable(
    root: Path,
    *,
    actor: str,
    role: str,
    peer: str | None,
    enforcement_mode: str,
    install_hooks: bool,
) -> int:
    resolved_peer = peer or default_peer(role)
    if resolved_peer == role:
        raise SystemExit("peer role must differ from role")
    with collab_lock(root):
        ensure_initialized(root)
        write_agent_config(
            root,
            actor=actor,
            role=role,
            peer=resolved_peer,
            enforcement_mode=enforcement_mode,
        )
    if install_hooks:
        install_git_hooks(root, role=role)
    current_task_id = load_role_state(root, role).get("current_task_id")
    record_role_sync(
        root,
        role=role,
        actor=actor,
        task_id=current_task_id,
        branch=None,
        head=None,
        note=f"enabled runtime ({enforcement_mode})",
        inbox_mtime=current_inbox_mtime(root, role),
        sync_source="manual",
        sync_reason="enable",
    )
    print(f"enabled agent runtime for {role} (actor={actor}, peer={resolved_peer}, enforce={enforcement_mode})")
    if install_hooks:
        print("installed git hooks")
    print("next: collab.py daemon up")
    print("next: collab.py doctor")
    return 0


def command_disable(root: Path, *, role: str | None = None, uninstall_hooks: bool) -> int:
    resolved_role = resolve_runtime_role(root, role)
    agent = load_agent_config(root, role=resolved_role) if resolved_role is not None else None
    if uninstall_hooks:
        uninstall_git_hooks(root)
    if resolved_role is not None and role_runtime_path(root, resolved_role).exists():
        role_runtime_path(root, resolved_role).unlink()
    active_role = load_active_runtime_role(root)
    if resolved_role is not None and active_role == resolved_role:
        remaining = [item for item in configured_runtime_roles(root) if item != resolved_role]
        set_active_runtime_role(root, remaining[0] if remaining else None)
    print("disabled agent runtime")
    if agent:
        print(f"removed agent config for {agent.get('role')}")
    return 0


def command_install_hooks(root: Path, *, role: str | None = None) -> int:
    resolved_role = resolve_runtime_role(root, role)
    manifest = install_git_hooks(root, role=resolved_role)
    print(f"installed {len(manifest.get('hooks', {}))} git hooks")
    return 0


def command_uninstall_hooks(root: Path) -> int:
    manifest = uninstall_git_hooks(root)
    print(f"restored {len(manifest.get('hooks', {}))} git hooks")
    return 0


def command_doctor(root: Path, *, role: str | None = None) -> int:
    ensure_initialized(root)
    git = git_context(root)
    configured_roles = configured_runtime_roles(root)
    active_role = load_active_runtime_role(root)
    resolved_role = resolve_runtime_role(root, role)
    agent = load_agent_config(root, role=resolved_role) if resolved_role is not None else None
    daemon = load_daemon_status(root, role=resolved_role) if resolved_role is not None else None
    hooks = load_hooks_manifest(root)

    print(f"repo_root: {root}")
    print(f"git_branch: {git.get('branch')}")
    print(f"git_head: {git.get('head')}")
    if git.get("error"):
        print(f"git_error: {git['error']}")
    print(f"runtime_roles: {', '.join(configured_roles) if configured_roles else 'none'}")
    print(f"active_runtime_role: {active_role}")

    if agent is None:
        print("agent: missing")
        return 1

    inspected_role = agent["role"]
    print(
        f"agent: role={agent['role']} actor={agent['actor']} peer={agent.get('peer')} "
        f"enforce={agent.get('enforcement_mode')}"
    )
    print(f"hooks: installed={len((hooks or {}).get('hooks', {}))}")
    if daemon is None:
        print("daemon: missing")
    else:
        daemon_state, daemon_age = daemon_health(root=root, role=inspected_role, payload=daemon)
        print(
            f"daemon: status={daemon.get('status')} run_mode={daemon.get('run_mode')} "
            f"pid={daemon.get('pid')} age_seconds={daemon_age} health={daemon_state}"
        )

    readiness = sync_readiness(root, actor=agent["actor"], role=inspected_role, context="doctor")
    role_state = readiness.get("role_state") or default_role_state()
    print(
        f"sync: last_sync_at={role_state.get('last_sync_at')} "
        f"last_sync_branch={role_state.get('last_sync_branch')} "
        f"last_sync_head={role_state.get('last_sync_head')} "
        f"last_sync_source={role_state.get('last_sync_source')}"
    )

    if readiness["issues"]:
        print("issues:")
        for issue in readiness["issues"]:
            print(f"  - {issue}")
        return 1 if readiness["mode"] != "off" else 0

    print("doctor: healthy")
    return 0


def command_git_event(root: Path, *, event: str, role: str | None = None) -> int:
    resolved_role = resolve_runtime_role(root, role)
    agent = load_agent_config(root, role=resolved_role)
    if agent is None:
        return 0
    actor = agent["actor"]
    runtime_role = agent["role"]
    if event == "pre-push":
        readiness = sync_readiness(root, actor=actor, role=runtime_role, context="pre-push")
        if readiness["issues"]:
            for issue in readiness["issues"]:
                print(f"pre-push: {issue}", file=sys.stderr)
        return 1 if readiness["blocked"] else 0

    current_task_id = load_role_state(root, runtime_role).get("current_task_id")
    record_role_sync(
        root,
        role=runtime_role,
        actor=actor,
        task_id=current_task_id,
        branch=None,
        head=None,
        note=f"hook: {event}",
        inbox_mtime=current_inbox_mtime(root, runtime_role),
        sync_source="hook",
        sync_reason=event,
    )
    return 0


def command_daemon_once(root: Path, *, role: str | None = None) -> int:
    resolved_role = resolve_runtime_role(root, role)
    agent = load_agent_config(root, role=resolved_role)
    if agent is None:
        raise SystemExit("agent runtime is not enabled")
    result = daemon_sync_cycle(
        root,
        actor=agent["actor"],
        role=agent["role"],
        note="daemon heartbeat",
        run_mode="oneshot",
    )
    daemon = result["daemon"]
    print(
        f"daemon synced role={daemon['role']} branch={daemon.get('last_observed_branch')} "
        f"head={daemon.get('last_observed_head')}"
    )
    return 0


def command_daemon_up(root: Path, *, role: str | None = None, interval: int) -> int:
    import time

    resolved_role = resolve_runtime_role(root, role)
    agent = load_agent_config(root, role=resolved_role)
    if agent is None:
        raise SystemExit("agent runtime is not enabled")

    stop_event = threading.Event()

    def _request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    lock_handle = daemon_lock_path(root, agent["role"]).open("a+b")
    daemon_locked = False
    try:
        daemon_locked = _try_acquire_platform_lock(lock_handle)
        if not daemon_locked:
            raise SystemExit(f"daemon already running for role {agent['role']}")
        print(
            f"daemon running for role={agent['role']} actor={agent['actor']} "
            f"(interval={interval}s, ctrl-c to stop)",
            flush=True,
        )
        try:
            daemon_sync_cycle(
                root,
                actor=agent["actor"],
                role=agent["role"],
                note="daemon started",
                run_mode="foreground",
            )
            while not stop_event.wait(interval):
                daemon_sync_cycle(
                    root,
                    actor=agent["actor"],
                    role=agent["role"],
                    run_mode="foreground",
                )
        finally:
            signal.signal(signal.SIGTERM, previous_sigterm)
            signal.signal(signal.SIGINT, previous_sigint)
            existing = load_daemon_status(root, role=agent["role"]) or {}
            existing["status"] = "stopped"
            existing["run_mode"] = "foreground"
            existing["last_heartbeat_at"] = utc_now()
            write_daemon_status(root, role=agent["role"], payload=existing)
            print("daemon stopped", flush=True)
    finally:
        try:
            if daemon_locked:
                _release_platform_lock(lock_handle)
        finally:
            lock_handle.close()
    return 0


def command_daemon_status(root: Path, *, role: str | None = None) -> int:
    resolved_role = resolve_runtime_role(root, role)
    payload = load_daemon_status(root, role=resolved_role)
    if payload is None:
        print("daemon: not running")
        return 1
    health, age = daemon_health(root=root, role=resolved_role, payload=payload)
    print(
        f"daemon: status={payload.get('status')} run_mode={payload.get('run_mode')} "
        f"role={payload.get('role')} actor={payload.get('actor')} "
        f"pid={payload.get('pid')} age_seconds={age} health={health}"
    )
    print(f"  last_observed_branch={payload.get('last_observed_branch')}")
    print(f"  last_observed_head={payload.get('last_observed_head')}")
    return 0 if daemon_is_fresh(payload) else 1


def command_daemon_stop(root: Path, *, role: str | None = None) -> int:
    resolved_role = resolve_runtime_role(root, role)
    payload = load_daemon_status(root, role=resolved_role)
    if payload is None or payload.get("pid") is None:
        print("daemon: no pid recorded")
        return 1
    if payload.get("run_mode") != "foreground":
        print("daemon: no foreground daemon recorded")
        return 1
    if not daemon_lock_is_held(root, resolved_role):
        print("daemon: no foreground daemon recorded")
        return 1
    pid = int(payload["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        raise SystemExit(f"failed to stop daemon pid {pid}: {exc}") from exc
    print(f"sent SIGTERM to daemon pid {pid}")
    return 0


def command_status(root: Path) -> int:
    state = build_refreshed_state(root, state=load_state(root), tasks=load_tasks(root))
    tasks = load_tasks(root)
    task_lookup = tasks_by_id(tasks)
    print(f"project: {state['project']}")
    print(f"protocol: {state['protocol']}")
    print(f"updated_at: {state['updated_at']}")
    configured_roles = configured_runtime_roles(root)
    active_role = load_active_runtime_role(root)
    hooks = load_hooks_manifest(root)
    if configured_roles:
        print(f"runtime_roles: {', '.join(configured_roles)}")
        print(f"active_runtime_role: {active_role}")
        for runtime_role in configured_roles:
            agent = load_agent_config(root, role=runtime_role)
            daemon = load_daemon_status(root, role=runtime_role)
            health, age = daemon_health(root=root, role=runtime_role, payload=daemon)
            if agent is not None:
                print(
                    f"runtime[{runtime_role}]: actor={agent.get('actor')} peer={agent.get('peer')} "
                    f"enforce={agent.get('enforcement_mode')} daemon_mode={daemon.get('run_mode') if daemon else None} "
                    f"daemon_health={health} daemon_age_seconds={age}"
                )
    if hooks is not None:
        print(f"hooks_installed: {len(hooks.get('hooks', {}))}")
    print("")
    print("summary:")
    for key in (
        "total",
        "proposed",
        "in_progress",
        "in_review",
        "changes_requested",
        "approved",
        "blocked",
        "done",
        "queued_for_architect",
        "queued_for_builder",
    ):
        print(f"  {key}: {state['summary'][key]}")
    print("")
    print("roles:")
    for role in ROLES:
        role_state = state["roles"][role]
        age_seconds = role_sync_age(role_state)
        freshness = "never" if age_seconds is None else ("STALE" if age_seconds > PEER_STALE_THRESHOLD_SECONDS else "fresh")
        print(
            f"  {role}: "
            f"queue_depth={role_state['queue_depth']} "
            f"current_task_id={role_state['current_task_id']} "
            f"last_sync_at={role_state.get('last_sync_at')} "
            f"last_sync_by={role_state.get('last_sync_by')} "
            f"last_sync_task_id={role_state.get('last_sync_task_id')} "
            f"last_sync_branch={role_state.get('last_sync_branch')} "
            f"last_sync_head={role_state.get('last_sync_head')} "
            f"last_sync_source={role_state.get('last_sync_source')} "
            f"sync_status={freshness}"
        )
        if age_seconds is not None:
            print(f"    sync_age_seconds={age_seconds}")
        if role_state.get("last_sync_reason"):
            print(f"    last_sync_reason={role_state['last_sync_reason']}")
        if role_state.get("last_sync_note"):
            print(f"    last_sync_note={role_state['last_sync_note']}")
        current_task_id = role_state.get("current_task_id")
        current_task = task_lookup.get(current_task_id) if current_task_id else None
        if current_task is not None:
            print(
                f"    current_task: {current_task['task_id']} "
                f"[{current_task['status']}] {current_task['title']}"
            )
            for line in task_context_lines(current_task, indent="      "):
                print(line)
    return 0


def command_next(root: Path, role: str) -> int:
    tasks = queued_tasks(load_tasks(root), role)
    if not tasks:
        print(f"no queued tasks for {role}")
        return 0
    for task in tasks:
        print(f"{task['task_id']} [{task['status']}] {task['title']}")
        for line in task_context_lines(
            task,
            include_files=True,
            include_acceptance=True,
        ):
            print(line)
    return 0


def command_sync(
    root: Path,
    *,
    role: str,
    actor: str,
    task_id: str | None,
    branch: str | None,
    note: str | None,
    max_age_seconds: int,
    check_stale: bool,
    stale_threshold_seconds: int,
) -> int:
    stale_tasks: list[dict[str, Any]] = []
    newly_flagged: list[dict[str, Any]] = []
    if check_stale:
        stale_tasks, newly_flagged = check_stale_tasks(
            root,
            actor=actor,
            threshold_seconds=stale_threshold_seconds,
        )

    state = build_refreshed_state(root, state=load_state(root), tasks=load_tasks(root))
    role_state = default_role_state()
    role_state.update(state["roles"].get(role, {}))
    previous_sync_at = parse_timestamp(role_state.get("last_sync_at"))
    previous_inbox_mtime = float(role_state.get("last_inbox_mtime") or 0.0)
    tasks = load_tasks(root)
    queue = queued_tasks(tasks, role)
    updates = task_updates_since(tasks, role=role, since=previous_sync_at)
    inbox_mtime = current_inbox_mtime(root, role)
    inbox_updated = inbox_mtime > previous_inbox_mtime
    checkpoint_task_id = canonical_task_id(task_id) or canonical_task_id(
        role_state.get("current_task_id")
    )

    print(f"sync {role} by {actor} at {utc_now()}")
    print(f"  queue_depth={len(queue)} current_task_id={role_state.get('current_task_id')}")
    if updates:
        print("  task updates since last sync:")
        for task in updates:
            print(f"    - {task['task_id']} [{task['status']}] {task['title']}")
    else:
        print("  no task updates since last sync")
    if inbox_updated:
        print("  inbox updated since last sync")
    else:
        print("  inbox unchanged since last sync")
    if check_stale:
        if stale_tasks:
            print(f"  stale tasks detected: {len(stale_tasks)} total, {len(newly_flagged)} newly flagged")
            for task in stale_tasks:
                print(f"    - {task['task_id']} [{task['status']}] {task['title']}")
        else:
            print("  no stale tasks detected")

    peer_role, peer_summary = peer_sync_summary(state, role=role, max_age_seconds=max_age_seconds)
    print(f"  peer {peer_role}: {peer_summary}")

    record_role_sync(
        root,
        role=role,
        actor=actor,
        task_id=checkpoint_task_id,
        branch=branch,
        head=None,
        note=note,
        inbox_mtime=inbox_mtime,
        sync_source="manual",
        sync_reason="manual sync",
    )
    return 0


def command_new_task(
    root: Path,
    *,
    title: str,
    created_by: str,
    assigned_role: str,
    source_plan: str | None,
    branch: str | None,
    files: list[str],
    acceptance: list[str],
    note: str | None,
    verification_cmd: list[str] | None,
) -> int:
    with collab_lock(root):
        tasks = load_tasks(root)
        state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
        task_number = int(state.get("last_task_number", 0)) + 1
        task_id = f"TASK-{task_number:03d}"
        task_branch = branch or f"collab/{task_id.lower()}-{slugify(title)[:48]}"
        now = utc_now()
        task = {
            "schema_version": TASK_SCHEMA_VERSION,
            "task_id": task_id,
            "title": title,
            "status": "proposed",
            "created_by": created_by,
            "assignee_role": assigned_role,
            "source_plan": source_plan,
            "branch": task_branch,
            "files": files,
            "acceptance": acceptance,
            "note": note,
            "created_at": now,
            "updated_at": now,
            "handoff": {},
            "review": {},
            "history": [],
            "verification_cmd": list(verification_cmd) if verification_cmd else None,
            "verification": {},
            "last_activity_at": now,
            "stale_flagged_at": None,
            "stale_reason": None,
            "depends_on": [],
        }
        append_history(task, actor=created_by, event="created", detail=f"assigned to {assigned_role}")
        atomic_write_json(task_file(root, task_id), task)

        tasks.append(task)
        state["last_task_number"] = task_number
        state = build_refreshed_state(root, state=state, tasks=tasks)
        state = auto_sync_actor_state(
            state,
            tasks,
            root=root,
            actor=created_by,
            task_id=task_id,
            branch=task_branch,
            head=None,
            note=f"auto: created {task_id}",
        )
        atomic_write_json(state_path(root), state)

    print(f"created {task_id} on branch {task_branch}")
    return 0


def command_claim(root: Path, *, task_id: str, actor: str) -> int:
    task_id = canonical_task_id(task_id) or task_id
    with collab_lock(root):
        tasks = load_tasks(root)
        task = tasks_by_id(tasks).get(task_id)
        if task is None:
            raise SystemExit(f"task not found: {task_id}")

        validate_transition(task, "in_progress")
        claimant_role = actor_role(actor) or task.get("assignee_role")
        task["assignee_role"] = claimant_role
        validate_required_fields(task, "in_progress")
        task["status"] = "in_progress"
        append_history(task, actor=actor, event="claimed", detail="work started")
        save_task(root, task, touch_activity=True)

        state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
        state = auto_sync_actor_state(
            state,
            tasks,
            root=root,
            actor=actor,
            task_id=task_id,
            branch=task.get("branch"),
            head=None,
            note=f"auto: claimed {task_id}",
        )
        atomic_write_json(state_path(root), state)

    print(f"claimed {task_id}")
    return 0


def persist_verification_result(root: Path, *, task: dict[str, Any], actor: str) -> dict[str, Any]:
    verification = run_verification(root, task)
    task["verification"] = verification
    result = "passed" if verification.get("passed") else "failed"
    detail = f"{result}: {render_command(verification.get('command') or [])}"
    if verification.get("output"):
        detail = f"{detail} ({verification['output']})"
    append_history(task, actor=actor, event="verify", detail=detail)
    save_task(root, task, touch_activity=True)
    return verification


def command_verify(root: Path, *, task_id: str, actor: str) -> int:
    task_id = canonical_task_id(task_id) or task_id
    with collab_lock(root):
        tasks = load_tasks(root)
        task = tasks_by_id(tasks).get(task_id)
        if task is None:
            raise SystemExit(f"task not found: {task_id}")
        if not task.get("verification_cmd"):
            raise SystemExit(f"{task_id} has no verification command configured")

        verification = persist_verification_result(root, task=task, actor=actor)
        state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
        state = auto_sync_actor_state(
            state,
            tasks,
            root=root,
            actor=actor,
            task_id=task_id,
            branch=task.get("branch"),
            head=None,
            note=f"auto: verified {task_id} ({verification_status(task)})",
        )
        atomic_write_json(state_path(root), state)

    print(f"verification {verification_status(task)} for {task_id}")
    return 0 if verification.get("passed") else 1


def command_handoff(
    root: Path,
    *,
    task_id: str,
    actor: str,
    to_role: str,
    summary: str,
    branch: str | None,
    pr_url: str | None,
    commit: str | None,
    skip_verify: bool,
    allow_unsynced: bool,
) -> int:
    task_id = canonical_task_id(task_id) or task_id
    with collab_lock(root):
        tasks = load_tasks(root)
        task = tasks_by_id(tasks).get(task_id)
        if task is None:
            raise SystemExit(f"task not found: {task_id}")
        readiness = ensure_sync_ready(
            root,
            actor=actor,
            role=actor_role(actor) or str(task.get("assignee_role") or "builder"),
            context="handoff",
            allow_unsynced=allow_unsynced,
        )

        validate_transition(task, "in_review")

        handoff_branch = branch or task.get("branch")
        handoff_verification: dict[str, Any] = {"status": "not_configured"}
        if task.get("verification_cmd"):
            if skip_verify:
                handoff_verification = {
                    "status": "skipped",
                    "at": utc_now(),
                    "by": actor,
                    "reason": "explicit --skip-verify override",
                }
                append_history(
                    task,
                    actor=actor,
                    event="verify_skipped",
                    detail="verification skipped via --skip-verify",
                )
                save_task(root, task, touch_activity=True)
            else:
                verification = persist_verification_result(root, task=task, actor=actor)
                handoff_verification = {
                    "status": verification_status(task),
                    "at": verification.get("ran_at"),
                    "by": actor,
                    "exit_code": verification.get("exit_code"),
                }
                if not verification.get("passed"):
                    state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
                    state = auto_sync_actor_state(
                        state,
                        tasks,
                        root=root,
                        actor=actor,
                        task_id=task_id,
                        branch=handoff_branch,
                        head=None,
                        note=f"auto: verification failed for {task_id}",
                    )
                    atomic_write_json(state_path(root), state)
                    output = verification.get("output") or "verification failed"
                    raise SystemExit(f"verification failed for {task_id}: {output}")

        task["assignee_role"] = to_role
        task.setdefault("handoff", {}).update(
            {
                "at": utc_now(),
                "by": actor,
                "summary": summary,
                "branch": handoff_branch,
                "pr_url": pr_url,
                "commit": commit,
                "verification": handoff_verification,
            }
        )
        validate_required_fields(task, "in_review")
        task["status"] = "in_review"
        if allow_unsynced and readiness["issues"]:
            task["handoff"]["sync_override"] = {
                "at": utc_now(),
                "by": actor,
                "issues": readiness["issues"],
            }
            append_history(task, actor=actor, event="sync_override", detail="handoff allowed while unsynced")
        append_history(task, actor=actor, event="handoff", detail=summary)
        save_task(root, task, touch_activity=True)

        state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
        state = auto_sync_actor_state(
            state,
            tasks,
            root=root,
            actor=actor,
            task_id=task_id,
            branch=handoff_branch,
            head=None,
            note=f"auto: handed off {task_id} to {to_role}",
        )
        atomic_write_json(state_path(root), state)

    print(f"handed off {task_id} to {to_role}")
    return 0


def command_review(
    root: Path,
    *,
    task_id: str,
    actor: str,
    decision: str,
    summary: str,
    findings: list[str],
    allow_unsynced: bool,
) -> int:
    task_id = canonical_task_id(task_id) or task_id
    with collab_lock(root):
        tasks = load_tasks(root)
        task = tasks_by_id(tasks).get(task_id)
        if task is None:
            raise SystemExit(f"task not found: {task_id}")
        readiness = ensure_sync_ready(
            root,
            actor=actor,
            role=actor_role(actor) or "architect",
            context="review",
            allow_unsynced=allow_unsynced,
        )
        if task["status"] != "in_review":
            raise SystemExit(f"cannot review task in status {task['status']}")

        if decision == "approve":
            new_status = "approved"
            assignee_role = "builder"
        elif decision == "changes_requested":
            new_status = "changes_requested"
            assignee_role = "builder"
        else:
            new_status = "blocked"
            assignee_role = "architect"

        validate_transition(task, new_status)

        timestamp = utc_now().replace(":", "").replace("+00:00", "Z")
        review_name = f"{task_id}-{timestamp}-{decision}.md"
        review_path = reviews_dir(root) / review_name
        atomic_write_text(
            review_path,
            render_review_markdown(
                task=task,
                actor=actor,
                decision=decision,
                summary=summary,
                findings=findings,
            ),
        )

        task["assignee_role"] = assignee_role
        task["review"] = {
            "at": utc_now(),
            "by": actor,
            "decision": decision,
            "summary": summary,
            "findings": findings,
            "file": str(review_path.relative_to(root)),
        }
        if allow_unsynced and readiness["issues"]:
            task["review"]["sync_override"] = {
                "at": utc_now(),
                "by": actor,
                "issues": readiness["issues"],
            }
            append_history(task, actor=actor, event="sync_override", detail="review allowed while unsynced")
        validate_required_fields(task, new_status)
        task["status"] = new_status
        append_history(task, actor=actor, event="review", detail=f"{decision}: {summary}")
        save_task(root, task, touch_activity=True)

        state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
        state = auto_sync_actor_state(
            state,
            tasks,
            root=root,
            actor=actor,
            task_id=task_id,
            branch=(task.get("handoff") or {}).get("branch") or task.get("branch"),
            head=None,
            note=f"auto: reviewed {task_id} ({decision})",
        )
        atomic_write_json(state_path(root), state)

    print(f"recorded review for {task_id}: {decision}")
    return 0


def command_complete(
    root: Path,
    *,
    task_id: str,
    actor: str,
    summary: str,
    allow_unsynced: bool,
) -> int:
    task_id = canonical_task_id(task_id) or task_id
    with collab_lock(root):
        tasks = load_tasks(root)
        task = tasks_by_id(tasks).get(task_id)
        if task is None:
            raise SystemExit(f"task not found: {task_id}")
        readiness = ensure_sync_ready(
            root,
            actor=actor,
            role=actor_role(actor) or "builder",
            context="complete",
            allow_unsynced=allow_unsynced,
        )

        validate_transition(task, "done")
        task["assignee_role"] = None
        validate_required_fields(task, "done")
        task["status"] = "done"
        if allow_unsynced and readiness["issues"]:
            append_history(task, actor=actor, event="sync_override", detail="complete allowed while unsynced")
        append_history(task, actor=actor, event="completed", detail=summary)
        save_task(root, task, touch_activity=True)

        state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
        state = auto_sync_actor_state(
            state,
            tasks,
            root=root,
            actor=actor,
            task_id=task_id,
            branch=(task.get("handoff") or {}).get("branch") or task.get("branch"),
            head=None,
            note=f"auto: completed {task_id}",
        )
        atomic_write_json(state_path(root), state)

    print(f"completed {task_id}")
    return 0


def command_message(
    root: Path,
    *,
    to_role: str,
    from_actor: str,
    subject: str,
    body: str,
) -> int:
    with collab_lock(root):
        append_inbox_entry(
            root,
            to_role=to_role,
            from_actor=from_actor,
            subject=subject,
            body=body,
        )
        tasks = load_tasks(root)
        state = build_refreshed_state(root, state=load_state(root), tasks=tasks)
        state = auto_sync_actor_state(
            state,
            tasks,
            root=root,
            actor=from_actor,
            task_id=None,
            branch=None,
            head=None,
            note=f"auto: messaged {to_role} ({subject})",
        )
        atomic_write_json(state_path(root), state)

    print(f"appended message to {inbox_path(root, to_role).relative_to(root)}")
    return 0


def command_check_stale(
    root: Path,
    *,
    actor: str,
    threshold_seconds: int,
) -> int:
    stale_tasks, newly_flagged = check_stale_tasks(
        root,
        actor=actor,
        threshold_seconds=threshold_seconds,
    )
    if not stale_tasks:
        print("no stale tasks detected")
        return 0

    print(f"stale tasks: {len(stale_tasks)} total, {len(newly_flagged)} newly flagged")
    for task in stale_tasks:
        print(f"- {task['task_id']} [{task['status']}] {task['title']}")
    return 0


def command_watch(
    root: Path,
    role: str,
    interval: int,
    *,
    actor: str | None,
    note: str | None,
    max_age_seconds: int,
    check_stale: bool,
    stale_threshold_seconds: int,
    stale_every: int,
) -> int:
    import time

    last_snapshot = ""
    cycle = 0
    print(f"watching queue for {role} (polling every {interval}s, ctrl-c to stop)", flush=True)
    try:
        while True:
            cycle += 1
            stale_tasks: list[dict[str, Any]] = []
            newly_flagged: list[dict[str, Any]] = []
            if check_stale and cycle % max(stale_every, 1) == 0:
                stale_actor = actor or f"{role}-watch"
                stale_tasks, newly_flagged = check_stale_tasks(
                    root,
                    actor=stale_actor,
                    threshold_seconds=stale_threshold_seconds,
                )

            state = build_refreshed_state(root, state=load_state(root), tasks=load_tasks(root))
            tasks = queued_tasks(load_tasks(root), role)
            inbox_mtime = current_inbox_mtime(root, role)
            current_task_id = tasks[0]["task_id"] if tasks else None

            if actor:
                record_role_sync(
                    root,
                    role=role,
                    actor=actor,
                    task_id=current_task_id,
                    branch=None,
                    head=None,
                    note=note,
                    inbox_mtime=inbox_mtime,
                    sync_source="command_auto",
                    sync_reason="watch heartbeat",
                )
                state = build_refreshed_state(root, state=load_state(root), tasks=load_tasks(root))

            task_bits = [
                f"{task['task_id']}:{task['status']}:{task.get('stale_flagged_at') or '-'}:{verification_status(task) or '-'}"
                for task in tasks
            ]
            snapshot = f"{len(tasks)}|{task_bits}|{inbox_mtime}"
            if snapshot != last_snapshot or newly_flagged:
                now = utc_now()
                if tasks:
                    for task in tasks:
                        print(f"[{now}] {task['task_id']} [{task['status']}] {task['title']}", flush=True)
                else:
                    print(f"[{now}] no queued tasks for {role}", flush=True)

                if inbox_mtime != 0 and (
                    last_snapshot == ""
                    or inbox_mtime > float(last_snapshot.rsplit("|", 1)[-1] or "0")
                ):
                    print(f"[{now}] inbox updated — check .collab/inbox/{role}.md", flush=True)

                if check_stale and stale_tasks:
                    print(
                        f"[{now}] stale tasks: {len(stale_tasks)} total, {len(newly_flagged)} newly flagged",
                        flush=True,
                    )

                peer_role, peer_summary = peer_sync_summary(
                    state,
                    role=role,
                    max_age_seconds=max_age_seconds,
                )
                print(f"[{now}] peer {peer_role}: {peer_summary}", flush=True)

                last_snapshot = snapshot
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped watching")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repo root that contains .collab/")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create missing .collab/ files")
    subparsers.add_parser("status", help="Show queue summary")

    enable = subparsers.add_parser("enable", help="Enable runtime identity, hooks, and enforcement policy")
    enable.add_argument("--actor", required=True)
    enable.add_argument("--role", required=True, choices=ROLES)
    enable.add_argument("--peer", choices=ROLES)
    enable.add_argument("--enforce", dest="enforcement_mode", default="warn", choices=ENFORCEMENT_MODES)
    enable.add_argument("--no-hooks", action="store_true", help="Write agent identity without installing git hooks")

    disable = subparsers.add_parser("disable", help="Disable runtime identity and optionally remove hooks")
    disable.add_argument("--role", choices=ROLES, help="Disable a specific runtime role; defaults to the active role")
    disable.add_argument("--keep-hooks", action="store_true", help="Leave installed git hooks in place")

    doctor = subparsers.add_parser("doctor", help="Inspect runtime identity, hook install, daemon health, and sync readiness")
    doctor.add_argument("--role", choices=ROLES, help="Inspect a specific runtime role")
    install_hooks = subparsers.add_parser("install-hooks", help="Install repo-local git hooks for auto-sync")
    install_hooks.add_argument("--role", choices=ROLES, help="Install hooks for a specific runtime role")
    subparsers.add_parser("uninstall-hooks", help="Restore previously chained git hooks")

    git_event = subparsers.add_parser("git-event", help=argparse.SUPPRESS)
    git_event.add_argument("--role", choices=ROLES)
    git_event.add_argument("--event", required=True, choices=HOOK_EVENTS)
    git_event.add_argument("hook_args", nargs="*")

    daemon = subparsers.add_parser("daemon", help="Run or inspect the sync daemon")
    daemon.add_argument("--role", choices=ROLES, help="Operate on a specific runtime role")
    daemon_subparsers = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_up = daemon_subparsers.add_parser("up", help="Run the daemon in the foreground")
    daemon_up.add_argument("--interval", type=int, default=DAEMON_INTERVAL_SECONDS)
    daemon_subparsers.add_parser("once", help="Run one daemon sync cycle")
    daemon_subparsers.add_parser("status", help="Show daemon heartbeat status")
    daemon_subparsers.add_parser("stop", help="Stop the recorded daemon pid")

    next_parser = subparsers.add_parser("next", help="Show queued tasks for a role")
    next_parser.add_argument("--role", required=True, choices=ROLES)

    sync = subparsers.add_parser("sync", help="Record a sync checkpoint and show what changed")
    sync.add_argument("--role", required=True, choices=ROLES)
    sync.add_argument("--actor", required=True)
    sync.add_argument("--task-id")
    sync.add_argument("--branch")
    sync.add_argument("--note")
    sync.add_argument(
        "--max-age-seconds",
        type=int,
        default=PEER_STALE_THRESHOLD_SECONDS,
        help=(
            "Warn when the peer role has not synced within this threshold "
            f"(default: {PEER_STALE_THRESHOLD_SECONDS})"
        ),
    )
    sync.add_argument(
        "--check-stale",
        action="store_true",
        help="Scan for stale in-progress or in-review tasks during sync",
    )
    sync.add_argument(
        "--stale-threshold-seconds",
        type=int,
        default=STALE_THRESHOLD_SECONDS,
        help=(
            "Flag tasks with no activity beyond this threshold "
            f"(default: {STALE_THRESHOLD_SECONDS})"
        ),
    )

    new_task = subparsers.add_parser("new-task", help="Create a new task")
    new_task.add_argument("title")
    new_task.add_argument("--created-by", required=True)
    new_task.add_argument("--assigned-role", default="builder", choices=ROLES)
    new_task.add_argument("--source-plan")
    new_task.add_argument("--branch")
    new_task.add_argument("--file", action="append", dest="files", default=[])
    new_task.add_argument("--accept", action="append", dest="acceptance", default=[])
    new_task.add_argument("--note")
    new_task.add_argument(
        "--verify-cmd",
        help=(
            "Verification command string; parsed with shlex into argv tokens and "
            "run in the current checkout, which must match the task branch"
        ),
    )
    new_task.add_argument(
        "--verify-arg",
        action="append",
        dest="verification_cmd",
        default=[],
        help="Verification command argv token; repeat for each token",
    )

    claim = subparsers.add_parser("claim", help="Claim a queued task")
    claim.add_argument("task_id")
    claim.add_argument("--actor", required=True)

    verify = subparsers.add_parser("verify", help="Run a task's verification command")
    verify.add_argument("task_id")
    verify.add_argument("--actor", required=True)

    handoff = subparsers.add_parser("handoff", help="Send work for review")
    handoff.add_argument("task_id")
    handoff.add_argument("--actor", required=True)
    handoff.add_argument("--to-role", default="architect", choices=ROLES)
    handoff.add_argument("--summary", required=True)
    handoff.add_argument("--branch")
    handoff.add_argument("--pr-url")
    handoff.add_argument("--commit")
    handoff.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip configured verification and record an explicit warning",
    )
    handoff.add_argument(
        "--allow-unsynced",
        action="store_true",
        help="Allow handoff while sync readiness checks are failing and record the override",
    )

    review = subparsers.add_parser("review", help="Record a review decision")
    review.add_argument("task_id")
    review.add_argument("--actor", required=True)
    review.add_argument("--decision", required=True, choices=REVIEW_DECISIONS)
    review.add_argument("--summary", required=True)
    review.add_argument("--finding", action="append", default=[])
    review.add_argument(
        "--allow-unsynced",
        action="store_true",
        help="Allow review while sync readiness checks are failing and record the override",
    )

    complete = subparsers.add_parser("complete", help="Mark a task done")
    complete.add_argument("task_id")
    complete.add_argument("--actor", required=True)
    complete.add_argument("--summary", required=True)
    complete.add_argument(
        "--allow-unsynced",
        action="store_true",
        help="Allow completion while sync readiness checks are failing and record the override",
    )

    message = subparsers.add_parser("message", help="Append a short inbox message")
    message.add_argument("--to", required=True, choices=ROLES)
    message.add_argument("--from-actor", required=True)
    message.add_argument("--subject", required=True)
    message.add_argument("--body", required=True)

    check_stale = subparsers.add_parser("check-stale", help="Flag stale tasks and notify the architect inbox")
    check_stale.add_argument("--actor", default="system")
    check_stale.add_argument(
        "--threshold-seconds",
        type=int,
        default=STALE_THRESHOLD_SECONDS,
        help=(
            "Flag tasks with no activity beyond this threshold "
            f"(default: {STALE_THRESHOLD_SECONDS})"
        ),
    )

    watch = subparsers.add_parser("watch", help="Poll for queue changes and print when state changes")
    watch.add_argument("--role", required=True, choices=ROLES)
    watch.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")
    watch.add_argument("--actor", help="Record sync heartbeats for this actor while watching")
    watch.add_argument("--note", default="watching queue", help="Checkpoint note to record when --actor is set")
    watch.add_argument(
        "--max-age-seconds",
        type=int,
        default=PEER_STALE_THRESHOLD_SECONDS,
        help=(
            "Warn when the peer role has not synced within this threshold "
            f"(default: {PEER_STALE_THRESHOLD_SECONDS})"
        ),
    )
    watch.add_argument(
        "--check-stale",
        action="store_true",
        help="Scan for stale in-progress or in-review tasks while watching",
    )
    watch.add_argument(
        "--stale-threshold-seconds",
        type=int,
        default=STALE_THRESHOLD_SECONDS,
        help=(
            "Flag tasks with no activity beyond this threshold "
            f"(default: {STALE_THRESHOLD_SECONDS})"
        ),
    )
    watch.add_argument(
        "--stale-every",
        type=int,
        default=1,
        help="Run stale-task checks every N polling cycles (default: 1)",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = repo_root(args.root)

    if args.command == "init":
        return command_init(root)
    if args.command == "status":
        return command_status(root)
    if args.command == "enable":
        return command_enable(
            root,
            actor=args.actor,
            role=args.role,
            peer=args.peer,
            enforcement_mode=args.enforcement_mode,
            install_hooks=not args.no_hooks,
        )
    if args.command == "disable":
        return command_disable(root, role=args.role, uninstall_hooks=not args.keep_hooks)
    if args.command == "doctor":
        return command_doctor(root, role=args.role)
    if args.command == "install-hooks":
        return command_install_hooks(root, role=args.role)
    if args.command == "uninstall-hooks":
        return command_uninstall_hooks(root)
    if args.command == "git-event":
        return command_git_event(root, event=args.event, role=args.role)
    if args.command == "daemon":
        if args.daemon_command == "up":
            return command_daemon_up(root, role=args.role, interval=args.interval)
        if args.daemon_command == "once":
            return command_daemon_once(root, role=args.role)
        if args.daemon_command == "status":
            return command_daemon_status(root, role=args.role)
        if args.daemon_command == "stop":
            return command_daemon_stop(root, role=args.role)
        raise SystemExit(f"unknown daemon command: {args.daemon_command}")
    if args.command == "next":
        return command_next(root, args.role)
    if args.command == "sync":
        return command_sync(
            root,
            role=args.role,
            actor=args.actor,
            task_id=args.task_id,
            branch=args.branch,
            note=args.note,
            max_age_seconds=args.max_age_seconds,
            check_stale=args.check_stale,
            stale_threshold_seconds=args.stale_threshold_seconds,
        )
    if args.command == "new-task":
        verification_cmd = args.verification_cmd or None
        if args.verify_cmd:
            if verification_cmd:
                raise SystemExit("use either --verify-cmd or repeated --verify-arg, not both")
            verification_cmd = shlex.split(args.verify_cmd)
        return command_new_task(
            root,
            title=args.title,
            created_by=args.created_by,
            assigned_role=args.assigned_role,
            source_plan=args.source_plan,
            branch=args.branch,
            files=args.files,
            acceptance=args.acceptance,
            note=args.note,
            verification_cmd=verification_cmd,
        )
    if args.command == "claim":
        return command_claim(root, task_id=args.task_id, actor=args.actor)
    if args.command == "verify":
        return command_verify(root, task_id=args.task_id, actor=args.actor)
    if args.command == "handoff":
        return command_handoff(
            root,
            task_id=args.task_id,
            actor=args.actor,
            to_role=args.to_role,
            summary=args.summary,
            branch=args.branch,
            pr_url=args.pr_url,
            commit=args.commit,
            skip_verify=args.skip_verify,
            allow_unsynced=args.allow_unsynced,
        )
    if args.command == "review":
        return command_review(
            root,
            task_id=args.task_id,
            actor=args.actor,
            decision=args.decision,
            summary=args.summary,
            findings=args.finding,
            allow_unsynced=args.allow_unsynced,
        )
    if args.command == "complete":
        return command_complete(
            root,
            task_id=args.task_id,
            actor=args.actor,
            summary=args.summary,
            allow_unsynced=args.allow_unsynced,
        )
    if args.command == "message":
        return command_message(
            root,
            to_role=args.to,
            from_actor=args.from_actor,
            subject=args.subject,
            body=args.body,
        )
    if args.command == "check-stale":
        return command_check_stale(
            root,
            actor=args.actor,
            threshold_seconds=args.threshold_seconds,
        )
    if args.command == "watch":
        return command_watch(
            root,
            args.role,
            args.interval,
            actor=args.actor,
            note=args.note,
            max_age_seconds=args.max_age_seconds,
            check_stale=args.check_stale,
            stale_threshold_seconds=args.stale_threshold_seconds,
            stale_every=args.stale_every,
        )
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

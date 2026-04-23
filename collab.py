#!/usr/bin/env python3
"""Repo-local async collaboration CLI for architect/builder agent workflows."""

from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
STATE_VERSION = 3
TASK_SCHEMA_VERSION = 1
STALE_THRESHOLD_SECONDS = 86_400
PEER_STALE_THRESHOLD_SECONDS = 180

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


@contextmanager
def collab_lock(root: Path):
    ensure_initialized(root)
    with lock_path(root).open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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
            "approved": 1,
            "proposed": 2,
            "in_progress": 3,
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


def update_role_sync(
    state: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    root: Path,
    role: str,
    actor: str,
    task_id: str | None,
    branch: str | None,
    note: str | None,
    inbox_mtime: float,
) -> dict[str, Any]:
    role_state = default_role_state()
    role_state.update(state["roles"].get(role, {}))
    checkpoint_task_id = canonical_task_id(task_id) or canonical_task_id(
        role_state.get("current_task_id")
    )
    role_state["last_sync_at"] = utc_now()
    role_state["last_sync_by"] = actor
    role_state["last_sync_task_id"] = checkpoint_task_id
    role_state["last_sync_branch"] = resolve_checkpoint_branch(
        tasks,
        task_id=checkpoint_task_id,
        branch=branch,
    )
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
    note: str,
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
        note=note,
        inbox_mtime=current_inbox_mtime(root, role),
    )


def record_role_sync(
    root: Path,
    *,
    role: str,
    actor: str,
    task_id: str | None,
    branch: str | None,
    note: str | None,
    inbox_mtime: float,
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
            note=note,
            inbox_mtime=inbox_mtime,
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


def command_status(root: Path) -> int:
    state = build_refreshed_state(root, state=load_state(root), tasks=load_tasks(root))
    tasks = load_tasks(root)
    task_lookup = tasks_by_id(tasks)
    print(f"project: {state['project']}")
    print(f"protocol: {state['protocol']}")
    print(f"updated_at: {state['updated_at']}")
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
            f"sync_status={freshness}"
        )
        if age_seconds is not None:
            print(f"    sync_age_seconds={age_seconds}")
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
        note=note,
        inbox_mtime=inbox_mtime,
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
) -> int:
    task_id = canonical_task_id(task_id) or task_id
    with collab_lock(root):
        tasks = load_tasks(root)
        task = tasks_by_id(tasks).get(task_id)
        if task is None:
            raise SystemExit(f"task not found: {task_id}")

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
) -> int:
    task_id = canonical_task_id(task_id) or task_id
    with collab_lock(root):
        tasks = load_tasks(root)
        task = tasks_by_id(tasks).get(task_id)
        if task is None:
            raise SystemExit(f"task not found: {task_id}")
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
            note=f"auto: reviewed {task_id} ({decision})",
        )
        atomic_write_json(state_path(root), state)

    print(f"recorded review for {task_id}: {decision}")
    return 0


def command_complete(root: Path, *, task_id: str, actor: str, summary: str) -> int:
    task_id = canonical_task_id(task_id) or task_id
    with collab_lock(root):
        tasks = load_tasks(root)
        task = tasks_by_id(tasks).get(task_id)
        if task is None:
            raise SystemExit(f"task not found: {task_id}")

        validate_transition(task, "done")
        task["assignee_role"] = None
        validate_required_fields(task, "done")
        task["status"] = "done"
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
                    note=note,
                    inbox_mtime=inbox_mtime,
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

    review = subparsers.add_parser("review", help="Record a review decision")
    review.add_argument("task_id")
    review.add_argument("--actor", required=True)
    review.add_argument("--decision", required=True, choices=REVIEW_DECISIONS)
    review.add_argument("--summary", required=True)
    review.add_argument("--finding", action="append", default=[])

    complete = subparsers.add_parser("complete", help="Mark a task done")
    complete.add_argument("task_id")
    complete.add_argument("--actor", required=True)
    complete.add_argument("--summary", required=True)

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
        )
    if args.command == "review":
        return command_review(
            root,
            task_id=args.task_id,
            actor=args.actor,
            decision=args.decision,
            summary=args.summary,
            findings=args.finding,
        )
    if args.command == "complete":
        return command_complete(
            root,
            task_id=args.task_id,
            actor=args.actor,
            summary=args.summary,
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

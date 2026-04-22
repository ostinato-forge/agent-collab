#!/usr/bin/env python3
"""Repo-local async collaboration CLI for architect/builder agent workflows."""

from __future__ import annotations

import argparse
import json
import re
import sys
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
STATE_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str, *, fallback: str = "task") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def repo_root(value: str | None) -> Path:
    return Path(value or ".").resolve()


def collab_dir(root: Path) -> Path:
    return root / ".collab"


def state_path(root: Path) -> Path:
    return collab_dir(root) / "state.json"


def tasks_dir(root: Path) -> Path:
    return collab_dir(root) / "tasks"


def inbox_path(root: Path, role: str) -> Path:
    return collab_dir(root) / "inbox" / f"{role}.md"


def reviews_dir(root: Path) -> Path:
    return collab_dir(root) / "reviews"


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
            "architect": {"current_task_id": None, "queue_depth": 0},
            "builder": {"current_task_id": None, "queue_depth": 0},
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_initialized(root: Path) -> None:
    collab = collab_dir(root)
    (collab / "inbox").mkdir(parents=True, exist_ok=True)
    (collab / "tasks").mkdir(parents=True, exist_ok=True)
    (collab / "reviews").mkdir(parents=True, exist_ok=True)
    (collab / "runtime").mkdir(parents=True, exist_ok=True)

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
        write_json(state_path(root), default_state(root.name))


def list_task_paths(root: Path) -> list[Path]:
    return sorted(
        path
        for path in tasks_dir(root).glob("TASK-*.json")
        if path.is_file()
    )


def load_tasks(root: Path) -> list[dict[str, Any]]:
    return [read_json(path) for path in list_task_paths(root)]


def task_file(root: Path, task_id: str) -> Path:
    return tasks_dir(root) / f"{task_id}.json"


def load_task(root: Path, task_id: str) -> dict[str, Any]:
    path = task_file(root, task_id)
    if not path.exists():
        raise SystemExit(f"task not found: {task_id}")
    return read_json(path)


def save_task(root: Path, task: dict[str, Any]) -> None:
    task["updated_at"] = utc_now()
    write_json(task_file(root, task["task_id"]), task)


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


def refresh_state(root: Path) -> dict[str, Any]:
    ensure_initialized(root)
    current = read_json(state_path(root))
    tasks = load_tasks(root)
    current["version"] = STATE_VERSION
    current["project"] = root.name
    current["summary"] = summary_for_tasks(tasks)
    current["updated_at"] = utc_now()
    current["roles"] = {
        role: {
            "current_task_id": (
                queued_tasks(tasks, role)[0]["task_id"] if queued_tasks(tasks, role) else None
            ),
            "queue_depth": len(queued_tasks(tasks, role)),
        }
        for role in ROLES
    }
    write_json(state_path(root), current)
    return current


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repo root that contains .collab/")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create missing .collab/ files")
    subparsers.add_parser("status", help="Show queue summary")

    next_parser = subparsers.add_parser("next", help="Show queued tasks for a role")
    next_parser.add_argument("--role", required=True, choices=ROLES)

    new_task = subparsers.add_parser("new-task", help="Create a new task")
    new_task.add_argument("title")
    new_task.add_argument("--created-by", required=True)
    new_task.add_argument("--assigned-role", default="builder", choices=ROLES)
    new_task.add_argument("--source-plan")
    new_task.add_argument("--branch")
    new_task.add_argument("--file", action="append", dest="files", default=[])
    new_task.add_argument("--accept", action="append", dest="acceptance", default=[])
    new_task.add_argument("--note")

    claim = subparsers.add_parser("claim", help="Claim a queued task")
    claim.add_argument("task_id")
    claim.add_argument("--actor", required=True)

    handoff = subparsers.add_parser("handoff", help="Send work for review")
    handoff.add_argument("task_id")
    handoff.add_argument("--actor", required=True)
    handoff.add_argument("--to-role", default="architect", choices=ROLES)
    handoff.add_argument("--summary", required=True)
    handoff.add_argument("--branch")
    handoff.add_argument("--pr-url")
    handoff.add_argument("--commit")

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

    watch = subparsers.add_parser("watch", help="Poll for queue changes and print when state changes")
    watch.add_argument("--role", required=True, choices=ROLES)
    watch.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60)")

    return parser.parse_args(argv)


def command_init(root: Path) -> int:
    ensure_initialized(root)
    refresh_state(root)
    print(f"initialized {collab_dir(root)}")
    return 0


def command_status(root: Path) -> int:
    state = refresh_state(root)
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
        print(
            f"  {role}: queue_depth={role_state['queue_depth']} current_task_id={role_state['current_task_id']}"
        )
    return 0


def command_next(root: Path, role: str) -> int:
    refresh_state(root)
    tasks = queued_tasks(load_tasks(root), role)
    if not tasks:
        print(f"no queued tasks for {role}")
        return 0
    for task in tasks:
        print(f"{task['task_id']} [{task['status']}] {task['title']}")
        if task.get("branch"):
            print(f"  branch: {task['branch']}")
        if task.get("source_plan"):
            print(f"  source_plan: {task['source_plan']}")
        for path in task.get("files", []):
            print(f"  file: {path}")
        for item in task.get("acceptance", []):
            print(f"  accept: {item}")
        if task.get("note"):
            print(f"  note: {task['note']}")
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
) -> int:
    state = refresh_state(root)
    task_number = int(state.get("last_task_number", 0)) + 1
    task_id = f"TASK-{task_number:03d}"
    task_branch = branch or f"collab/{task_id.lower()}-{slugify(title)[:48]}"
    now = utc_now()
    task = {
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
    }
    append_history(task, actor=created_by, event="created", detail=f"assigned to {assigned_role}")
    save_task(root, task)
    state["last_task_number"] = task_number
    write_json(state_path(root), state)
    refresh_state(root)
    print(f"created {task_id} on branch {task_branch}")
    return 0


def command_claim(root: Path, *, task_id: str, actor: str) -> int:
    task = load_task(root, task_id)
    if task["status"] not in {"proposed", "changes_requested", "approved"}:
        raise SystemExit(f"cannot claim task in status {task['status']}")
    task["status"] = "in_progress"
    append_history(task, actor=actor, event="claimed", detail="work started")
    save_task(root, task)
    refresh_state(root)
    print(f"claimed {task_id}")
    return 0


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
) -> int:
    task = load_task(root, task_id)
    if task["status"] not in {"in_progress", "changes_requested"}:
        raise SystemExit(f"cannot hand off task in status {task['status']}")
    task["status"] = "in_review"
    task["assignee_role"] = to_role
    task.setdefault("handoff", {}).update(
        {
            "at": utc_now(),
            "by": actor,
            "summary": summary,
            "branch": branch or task.get("branch"),
            "pr_url": pr_url,
            "commit": commit,
        }
    )
    append_history(task, actor=actor, event="handoff", detail=summary)
    save_task(root, task)
    refresh_state(root)
    print(f"handed off {task_id} to {to_role}")
    return 0


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
    handoff = task.get("handoff") or {}
    if handoff:
        lines.extend(["## Handoff Context", ""])
        for key in ("branch", "pr_url", "commit", "summary"):
            value = handoff.get(key)
            if value:
                lines.append(f"- {key}: {value}")
        lines.append("")
    return "\n".join(lines)


def command_review(
    root: Path,
    *,
    task_id: str,
    actor: str,
    decision: str,
    summary: str,
    findings: list[str],
) -> int:
    task = load_task(root, task_id)
    if task["status"] != "in_review":
        raise SystemExit(f"cannot review task in status {task['status']}")

    if decision == "approve":
        status = "approved"
        assignee_role = "builder"
    elif decision == "changes_requested":
        status = "changes_requested"
        assignee_role = "builder"
    else:
        status = "blocked"
        assignee_role = "architect"

    timestamp = utc_now().replace(":", "").replace("+00:00", "Z")
    review_name = f"{task_id}-{timestamp}-{decision}.md"
    review_path = reviews_dir(root) / review_name
    review_path.write_text(
        render_review_markdown(
            task=task,
            actor=actor,
            decision=decision,
            summary=summary,
            findings=findings,
        ),
        encoding="utf-8",
    )

    task["status"] = status
    task["assignee_role"] = assignee_role
    task["review"] = {
        "at": utc_now(),
        "by": actor,
        "decision": decision,
        "summary": summary,
        "findings": findings,
        "file": str(review_path.relative_to(root)),
    }
    append_history(task, actor=actor, event="review", detail=f"{decision}: {summary}")
    save_task(root, task)
    refresh_state(root)
    print(f"recorded review for {task_id}: {decision}")
    return 0


def command_complete(root: Path, *, task_id: str, actor: str, summary: str) -> int:
    task = load_task(root, task_id)
    if task["status"] not in {"approved", "blocked"}:
        raise SystemExit(
            f"cannot complete task in status {task['status']}; expected approved or blocked"
        )
    task["status"] = "done"
    task["assignee_role"] = None
    append_history(task, actor=actor, event="completed", detail=summary)
    save_task(root, task)
    refresh_state(root)
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
    ensure_initialized(root)
    entry = (
        f"\n## {utc_now()} | from {from_actor}\n\n"
        f"Subject: {subject}\n\n"
        f"{body}\n"
    )
    inbox = inbox_path(root, to_role)
    with inbox.open("a", encoding="utf-8") as handle:
        handle.write(entry)
    refresh_state(root)
    print(f"appended message to {inbox.relative_to(root)}")
    return 0


def command_watch(root: Path, role: str, interval: int) -> int:
    import time

    last_snapshot = ""
    print(f"watching queue for {role} (polling every {interval}s, ctrl-c to stop)", flush=True)
    try:
        while True:
            refresh_state(root)
            tasks = queued_tasks(load_tasks(root), role)
            inbox = inbox_path(root, role)
            inbox_mtime = inbox.stat().st_mtime if inbox.exists() else 0

            snapshot = f"{len(tasks)}|{[t['task_id'] + ':' + t['status'] for t in tasks]}|{inbox_mtime}"
            if snapshot != last_snapshot:
                now = utc_now()
                if tasks:
                    for task in tasks:
                        print(f"[{now}] {task['task_id']} [{task['status']}] {task['title']}", flush=True)
                else:
                    print(f"[{now}] no queued tasks for {role}", flush=True)

                if inbox_mtime != 0 and (last_snapshot == "" or inbox_mtime > float(last_snapshot.rsplit("|", 1)[-1] or "0")):
                    print(f"[{now}] inbox updated — check .collab/inbox/{role}.md", flush=True)

                last_snapshot = snapshot
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped watching")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = repo_root(args.root)

    if args.command == "init":
        return command_init(root)
    if args.command == "status":
        return command_status(root)
    if args.command == "next":
        return command_next(root, args.role)
    if args.command == "new-task":
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
        )
    if args.command == "claim":
        return command_claim(root, task_id=args.task_id, actor=args.actor)
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
    if args.command == "watch":
        return command_watch(root, args.role, args.interval)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

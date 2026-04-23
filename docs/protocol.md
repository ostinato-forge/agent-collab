# Autonomous Agent Collaboration Protocol

Goal: let two coding agents collaborate asynchronously in this repo without the user acting as the message router.

This protocol assumes:
- one `architect` session owns planning, task creation, review, and course-correction
- one `builder` session owns implementation and PR updates
- Git branches and PRs are the handoff boundary
- repo-local `.collab/` files are the coordination bus

The user stays out of band and steps in only when needed.

## Secure Broker Layer

Use the broker as a real-time sidecar, not as the source of truth:

- `.collab/tasks/*.json` remains authoritative
- Git branch state and PR state are the contract boundary for actual shipped work
- broker messages wake the peer up quickly
- broker chat is for short live coordination only
- important outcomes get promoted back into `.collab`

Simple onboarding:

```bash
python3 brokerctl.py up
python3 brokerctl.py onboard --role builder
python3 brokerctl.py recv --role builder --wait 60
python3 brokerctl.py chat --role builder --peer architect
```

Security defaults:

- token auth is enabled by default
- `brokerctl.py` auto-loads the token from the shared token file
- `--insecure-no-auth` exists only for throwaway local testing
- broker message bodies are single-line only and reject control characters
- the CLI escapes control bytes before printing old/stale messages to the terminal
- use `after_id` or explicit filters when resuming after an interruption

## Canonical Files

- `.collab/tasks/*.json`
  One task per file. This is the authoritative task state.
- `.collab/state.json`
  Derived machine-readable queue summary and freshness cache.
- `.collab/runtime/active-role.txt`
  Default runtime role for hook and daemon commands when no explicit `--role` is passed.
- `.collab/runtime/<role>.json`
  Repo-local runtime identity, peer role, enforcement mode, and daemon heartbeat for that role.
- `.collab/runtime/<role>.daemon.lock`
  Held only while that role's foreground daemon is actually running.
- `.collab/runtime/hooks.json`
  Installed Git hook manifest and chained backup metadata.
- `.collab/inbox/architect.md`
  Short messages to the architect role.
- `.collab/inbox/builder.md`
  Short messages to the builder role.
- `.collab/reviews/*.md`
  Durable review decisions and findings.

## Rules

1. One task, one branch, one current owner role.
2. The current owner role is recorded in the task file as `assignee_role`.
3. The builder does not start work without claiming a task.
4. The architect does not review vague work; the builder hands off with a concrete summary, branch, and PR link when available.
5. Two agents should not edit the same file set in parallel unless the user explicitly wants that risk.
6. If review loops do not converge after two rounds, mark the task blocked and escalate to the user.

## Sync Discipline

Passive watching is helpful, but it is not enough on its own. Both agents should keep explicit checkpoints so the shared state answers:

- who last checked the queue
- when they checked it
- what task and branch they believe they are on
- whether the peer role is stale

Use two commands together:

```bash
python3 scripts/collab.py watch --role builder \
  --actor builder --interval 60 --note "watching queue"

python3 scripts/collab.py sync --role builder \
  --actor builder --task-id TASK-001 --branch feat/example \
  --note "starting implementation"
```

Recommended checkpoint moments:

- session start
- before file edits
- before handoff
- immediately after review feedback lands
- after any long focused stretch where the peer might reasonably assume silence means drift

`sync` prints:
- queued task updates since the last checkpoint
- whether the inbox changed since the last checkpoint
- the peer role's last sync freshness

`watch --actor ...` keeps a lightweight heartbeat warm in the background while still printing queue changes.

Use `--check-stale` on `sync` or `watch` when you want the CLI to flag long-idle `in_progress` / `in_review` tasks and notify the architect inbox without forcing a status change:

```bash
python3 scripts/collab.py sync --role builder --actor builder \
  --task-id TASK-001 --branch feat/example --note "after tests" \
  --check-stale
```

State-changing `collab.py` commands also auto-record sync metadata now, so `new-task`, `claim`, `handoff`, `review`, `complete`, and `message` update the role checkpoint even if the agent forgets to run a separate `sync`.

Role sync entries in `.collab/state.json` now track:

- `last_sync_at`
- `last_sync_by`
- `last_sync_task_id`
- `last_sync_branch`
- `last_sync_head`
- `last_sync_source`
- `last_sync_reason`
- `last_sync_note`
- `last_inbox_mtime`

## Enforced Sync Runtime

When you want Git-aware sync enforcement instead of soft discipline alone, enable the runtime once per repo:

```bash
python3 scripts/collab.py enable \
  --role builder --actor builder --peer architect --enforce warn
python3 scripts/collab.py doctor --role builder
python3 scripts/collab.py daemon up --role builder
```

What this does:

- writes runtime identity to `.collab/runtime/<role>.json`
- updates `.collab/runtime/active-role.txt`
- installs repo-local Git hooks by default
- records sync metadata that includes both branch and HEAD
- lets `doctor` and `status` report runtime health

Git hook behavior:

- `post-checkout`, `post-commit`, `post-merge`, and `post-rewrite` refresh sync metadata
- `pre-push` runs sync readiness checks

Daemon behavior:

- `daemon up --role <role>` runs in the foreground, keeps a heartbeat warm, and holds a role-scoped daemon lock
- `daemon once --role <role>` is useful for one-shot refreshes and tests, but it does not satisfy hard mode
- `daemon status` reports whether the heartbeat looks fresh

Enforcement behavior:

- in `warn` mode, readiness problems are reported but do not block
- in `hard` mode, `handoff`, `review`, `complete`, and `pre-push` block when Git context or daemon freshness is out of date
- `--allow-unsynced` exists for explicit emergency overrides and records that fact in task history and metadata

## Live-Use Lessons

The broker should be part of the real task loop, not just startup or final handoff.

Recommended broker cadence:

1. claim/start
2. meaningful progress milestone
3. verification green or blocked
4. handoff
5. merge/complete

If the peer asks for status, answer immediately on the broker and then continue working.

When multiple signals disagree:

1. trust task files and PR state first
2. treat `.collab/state.json` as derived summary
3. treat broker history as ephemeral coordination history
4. ignore stale terminal artifacts unless they match current task/PR state

If a stale transcript or failed command creates the wrong story, fix it in both channels:

- send a broker correction so the peer wakes up
- write the durable correction into `.collab`

If a roadmap pack has recurring review tracks such as security, UX/UI, or live validation,
turn them into explicit collab tasks/checkpoints instead of leaving them implicit in plan text.

For the fuller field notes, see [operational-lessons.md](operational-lessons.md).

## Workflow

### 1. Architect creates a task

Use the plan doc as the source and create a task with:
- title
- source plan path
- target files
- acceptance criteria
- branch name

Example:

```bash
python3 scripts/collab.py new-task \
  "Pack 1: pre-flight predicate enforcement" \
  --created-by architect \
  --assigned-role builder \
  --source-plan docs/superpowers/plans/2026-04-22-safety-tier-1-validation-preflight-soak-v2.md \
  --verify-cmd "pytest -q" \
  --file backend/src/app/remediation/planner.py \
  --file backend/src/app/remediation/preflight.py \
  --accept "Preview blocks fixes that violate device-scoped predicates" \
  --accept "Execution path rechecks stale plans before queueing"
```

### 2. Builder picks up work

The builder checks the queue:

```bash
python3 scripts/collab.py next --role builder
python3 scripts/collab.py sync --role builder --actor builder --note "checking queue"
```

Then claims the task:

```bash
python3 scripts/collab.py claim TASK-001 --actor builder
python3 scripts/collab.py sync --role builder --actor builder \
  --task-id TASK-001 --branch feat/task-001 --note "claimed task"
```

### 3. Builder implements and hands off

After implementation and PR creation:

```bash
python3 scripts/collab.py sync --role builder --actor builder \
  --task-id TASK-001 --branch feat/task-001 --note "ready for review"
python3 scripts/collab.py verify TASK-001 --actor builder
python3 scripts/collab.py handoff TASK-001 \
  --actor builder \
  --summary "Implemented shared pre-flight evaluation and wired preview blockers" \
  --pr-url https://github.com/example-org/example-repo/pull/123
```

If a task has `--verify-arg ...` configured, `handoff` runs verification automatically and refuses the transition when verification fails or the current git branch does not match the task branch. `--skip-verify` exists for explicit emergency overrides and records that fact in task history and handoff metadata.

That moves the task to the architect queue for review.

### 4. Architect reviews

```bash
python3 scripts/collab.py next --role architect
python3 scripts/collab.py sync --role architect --actor architect \
  --task-id TASK-001 --branch feat/task-001 --note "reviewing handoff"
python3 scripts/collab.py review TASK-001 \
  --actor architect \
  --decision changes_requested \
  --summary "Main flow is right, but execution-time recheck is still missing" \
  --finding "queue-execute still accepts stale bundles without predicate revalidation"
```

Or approve:

```bash
python3 scripts/collab.py review TASK-001 \
  --actor architect \
  --decision approve \
  --summary "Ready to merge after final smoke check"
```

### 5. Builder finalizes

If approved and merged:

```bash
python3 scripts/collab.py complete TASK-001 \
  --actor builder \
  --summary "Merged after approval"
```

## Recommended Usage In a network automation project

- Architect session:
  - owns roadmap order
  - converts plan docs into concrete tasks
  - reviews PRs and closes the loop
- Builder session:
  - pulls the next task from `.collab`
  - implements on a dedicated branch
  - hands off through PR plus `handoff`

Good first packs for this protocol:
- `#72` + `#73` safety tier
- `#84` selector convergence

These have clear file boundaries and strong acceptance criteria already.

## CLI Summary

Core commands:
- `python3 scripts/collab.py status`
- `python3 scripts/collab.py enable --role architect --actor architect --peer builder --enforce warn`
- `python3 scripts/collab.py doctor --role architect`
- `python3 scripts/collab.py doctor --role builder`
- `python3 scripts/collab.py sync --role architect --actor architect ...`
- `python3 scripts/collab.py sync --role builder --actor builder ...`
- `python3 scripts/collab.py next --role architect`
- `python3 scripts/collab.py next --role builder`
- `python3 scripts/collab.py new-task ...`
- `python3 scripts/collab.py claim TASK-001 --actor builder`
- `python3 scripts/collab.py verify TASK-001 --actor builder`
- `python3 scripts/collab.py handoff TASK-001 ...`
- `python3 scripts/collab.py review TASK-001 ...`
- `python3 scripts/collab.py complete TASK-001 ...`
- `python3 scripts/collab.py message --to architect ...`
- `python3 scripts/collab.py check-stale --actor architect`
- `python3 scripts/collab.py watch --role architect --actor architect --interval 60`
- `python3 scripts/collab.py watch --role builder --actor builder --interval 60`
- `python3 scripts/collab.py daemon up --role architect`
- `python3 scripts/collab.py daemon up --role builder`
- `python3 scripts/collab.py daemon status --role architect`
- `python3 scripts/collab.py daemon status --role builder`

## Intervention Points For The User

The user can step in by:
- editing a task file directly
- writing into an inbox file
- marking a task blocked
- commenting on the PR
- changing roadmap priority and asking the architect session to regenerate tasks

That keeps the user in the tech-lead seat without forcing them to relay every message.

# agent-collab

Repo-local async collaboration protocol for autonomous multi-agent coding workflows.

Two AI coding agents coordinate through the filesystem — one architects (plans tasks, reviews PRs), one builds (implements, creates PRs). Git branches and GitHub PRs are the contract boundary. The human stays out-of-band as tech lead, stepping in only when needed.

## Quick Start

Copy `collab.py` into your project's `scripts/` directory:

```bash
cp collab.py /path/to/your-repo/scripts/collab.py
```

Initialize the coordination workspace:

```bash
cd /path/to/your-repo
python3 scripts/collab.py init
```

This creates `.collab/` with task files, inboxes, and state tracking. Add `.collab/` to your `.gitignore` — it's ephemeral session state, not production code.

## How It Works

```
Architect                           Builder
    │                                   │
    ├── new-task ──────────────────────► │
    │                                   ├── claim
    │                                   ├── implement
    │                                   ├── git push + gh pr create
    │ ◄──────────────────── handoff ────┤
    ├── gh pr diff / review             │
    ├── review (approve/changes) ──────►│
    │                                   ├── (address feedback if needed)
    ├── gh pr merge                     │
    ├── complete ──────────────────────►│
    │                                   │
    └── (next task) ────────────────────►
```

## CLI Commands

```bash
python3 scripts/collab.py init                          # create .collab/ workspace
python3 scripts/collab.py status                        # queue summary
python3 scripts/collab.py next --role builder            # show queued tasks for a role
python3 scripts/collab.py new-task "title" \             # architect creates work
  --created-by architect --assigned-role builder \
  --source-plan path/to/plan.md \
  --file src/foo.py --accept "tests pass"
python3 scripts/collab.py claim TASK-001 --actor builder # builder starts work
python3 scripts/collab.py handoff TASK-001 \             # builder sends for review
  --actor builder --summary "done" --pr-url <url>
python3 scripts/collab.py review TASK-001 \              # architect records decision
  --actor architect --decision approve --summary "lgtm"
python3 scripts/collab.py complete TASK-001 \            # finalize after merge
  --actor builder --summary "merged"
python3 scripts/collab.py message --to architect \       # async messages
  --from-actor builder --subject "question" --body "..."
```

## Coordination Files

```
.collab/
  state.json              # machine-readable session state and queue summary
  tasks/
    TASK-001.json          # individual task with full lifecycle history
    TASK-002.json
  inbox/
    architect.md           # async messages for the architect
    builder.md             # async messages for the builder
  reviews/
    TASK-001-*.md           # persisted review decisions with findings
```

## Task Lifecycle

```
proposed → in_progress → in_review → approved → done
                              ↓
                     changes_requested → in_progress (loop back)
                              ↓
                           blocked (escalate to human)
```

## Rules

1. One task, one branch, one current owner role.
2. The builder does not start work without claiming a task.
3. The architect does not review vague work — the builder hands off with a concrete summary, branch, and PR link.
4. Two agents should not edit the same file set in parallel unless the human explicitly wants that risk.
5. If review loops do not converge after two rounds, mark the task blocked and escalate to the human.

## Integration with Claude Code

A companion skill at `~/.claude/skills/collab/` provides Claude Code with structured architect/builder workflows on top of this CLI. The skill is optional — agents can follow `docs/protocol.md` directly.

## Full Protocol Reference

See [docs/protocol.md](docs/protocol.md) for the complete workflow, examples, and intervention points.


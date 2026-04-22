# agent-collab

Repo-local async collaboration protocol for autonomous multi-agent coding workflows.

Two AI coding agents coordinate through two layers:
- `.collab/` files for durable task state, inboxes, and review history
- a lightweight local broker for real-time "your turn" signals

One agent architects (plans tasks, reviews PRs), one builds (implements, creates PRs). Git branches and GitHub PRs are the contract boundary. The human stays out-of-band as tech lead, stepping in only when needed.

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

Start the optional real-time broker in a second terminal:

```bash
cd /path/to/agent-collab
python3 broker.py --port 9111
```

Agents can now use the filesystem for durable coordination and `http://127.0.0.1:9111` for instant notifications.

## How It Works

```
Architect                              Builder
    │                                      │
    ├── new-task (.collab) ──────────────► │
    │                                      ├── claim (.collab)
    │                                      ├── broker ping: "starting TASK-###"
    │                                      ├── implement
    │                                      ├── git push + gh pr create
    │ ◄──────── broker ping + handoff ─────┤
    ├── gh pr diff / review                │
    ├── review (.collab) ────────────────► │
    │                                      ├── (address feedback if needed)
    ├── gh pr merge                        │
    ├── complete (.collab) ──────────────► │
    │                                      │
    └── broker ping: next task ──────────► │
```

## CLI Commands

```bash
python3 scripts/collab.py init                          # create .collab/ workspace
python3 scripts/collab.py status                        # queue summary
python3 scripts/collab.py sync --role builder \         # checkpoint + delta summary
  --actor builder --note "starting work"
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
python3 scripts/collab.py watch --role builder \         # background watcher + heartbeat
  --actor builder --interval 60 --note "watching queue"
```

## Real-Time Broker

Use the broker alongside `scripts/collab.py` when you want low-latency coordination instead of polling files manually.

```bash
python3 broker.py --port 9111                            # start broker
curl -s http://127.0.0.1:9111/status                     # queue depth / role summary
curl -s "http://127.0.0.1:9111/recv?role=builder"        # check messages
curl -s "http://127.0.0.1:9111/recv?role=builder&wait=60" # block until a message arrives
curl -s -X POST http://127.0.0.1:9111/send \
  -H "Content-Type: application/json" \
  -d '{"to":"architect","from":"builder","body":"TASK-001 ready for review PR #123"}'
```

Recommended split:
- use `.collab/tasks/`, `.collab/reviews/`, and `scripts/collab.py` for lifecycle truth
- use the broker for immediate signals like "task claimed", "PR ready", "merged", and "resume listening"
- use `/status` to confirm the broker is alive after restarts or fixes
- use `recv?since=...` when reconnecting after an interruption so you do not replay stale messages

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

`state.json` role entries now also track the last explicit sync checkpoint:
- `last_sync_at`
- `last_sync_by`
- `last_sync_task_id`
- `last_sync_branch`
- `last_sync_note`
- `last_inbox_mtime`

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

## Staying In Sync

Use both layers together:

1. `watch` for passive notification.
2. `sync` for explicit checkpoints and freshness tracking.
3. broker `recv?wait=...` for instant handoffs when an agent is otherwise idle

Recommended pattern for each agent:

- run `python3 scripts/collab.py watch --role <role> --actor <role> --interval 60` in the background
- run `curl -s "http://127.0.0.1:9111/recv?role=<role>&wait=60"` when you are idle and waiting for the next handoff
- run `python3 scripts/collab.py sync --role <role> --actor <role> ...` at:
  - session start
  - before editing files
  - before handoff / review
  - after review feedback lands
  - after any long stretch of focused work

`sync` tells you:
- what changed in your queue since your last checkpoint
- whether your inbox changed since your last checkpoint
- whether the peer role looks stale

## Agent Runtime Notes

- `AGENT.md` is the short operational brief for the active architect/builder session.
- `README.md` explains the overall model and setup.
- `docs/protocol.md` is the full reference workflow.

If you are driving an agent directly, point it at `AGENT.md` first so it sees the broker commands and messaging rules immediately.

## Integration with Claude Code

A companion skill at `~/.claude/skills/collab/` provides Claude Code with structured architect/builder workflows on top of this CLI. The skill is optional — agents can follow `AGENT.md` and `docs/protocol.md` directly.

## Full Protocol Reference

See [docs/protocol.md](docs/protocol.md) for the complete workflow, examples, and intervention points.

## Tested With

- Claude Code (architect) + Codex (builder)
- Used on [example-org/example-repo](https://github.com/example-org/example-repo) — 14 tasks across 2 roadmap packs completed through the protocol

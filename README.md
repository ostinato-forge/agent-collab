# agent-collab

> **Status:** The protocol, the broker, and the CLI work, and the 44 tests pass from a fresh clone. Built and used on one private product in spring 2026; the architect and builder split it demonstrates moved into a larger private studio. Stable; not under active development.

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

If you want enforced sync with Git hooks and daemon-backed freshness checks, enable the runtime once per repo:

```bash
python3 scripts/collab.py enable --role builder --actor builder --peer architect --enforce warn
python3 scripts/collab.py doctor --role builder
python3 scripts/collab.py daemon up --role builder
```

`enable` writes repo-local runtime identity under `.collab/runtime/`, one file per role, installs Git hooks by default, and records an initial sync checkpoint. `doctor` shows whether identity, hooks, daemon heartbeat, and sync readiness are healthy for the selected role.

Start the secure real-time broker in a second terminal:

```bash
cd /path/to/agent-collab
python3 brokerctl.py up
```

Agents can now use the filesystem for durable coordination and `http://127.0.0.1:9111` for instant notifications.

Prefer the local wrapper instead of raw `curl` when driving agents from a restricted runtime:

```bash
python3 brokerctl.py onboard --role builder
python3 brokerctl.py start-session --role builder --repo /path/to/your-repo
python3 brokerctl.py status
python3 brokerctl.py recv --role builder --wait 60
python3 brokerctl.py send --to architect --from-role builder --body "TASK-001 ready for review PR #123"
python3 brokerctl.py chat --role builder --peer architect
```

`brokerctl.py up` enables token auth by default and writes the token to a per-user
token file under `/tmp` so other local agents can connect without re-copying secrets
by hand.

## Commit Identity Hygiene

When this protocol is used inside `netauto-labs`, commits should resolve to the
approved GitHub-linked personal address for the org rather than a corporate work
address. Check `git config user.email` before committing, and prefer an org-specific
Git include so the safe identity is applied automatically by remote URL.

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
python3 scripts/collab.py enable \                      # install runtime identity + hooks
  --role builder --actor builder --peer architect --enforce warn
python3 scripts/collab.py doctor --role builder         # inspect runtime identity + sync health
python3 scripts/collab.py sync --role builder \         # checkpoint + delta summary
  --actor builder --note "starting work"
python3 scripts/collab.py next --role builder            # show queued tasks for a role
python3 scripts/collab.py new-task "title" \             # architect creates work
  --created-by architect --assigned-role builder \
  --verify-cmd "pytest -q" \
  --source-plan path/to/plan.md \
  --file src/foo.py --accept "tests pass"
python3 scripts/collab.py claim TASK-001 --actor builder # builder starts work
python3 scripts/collab.py verify TASK-001 --actor builder # optional explicit verification run
python3 scripts/collab.py handoff TASK-001 \             # builder sends for review
  --actor builder --summary "done" --pr-url <url>
python3 scripts/collab.py review TASK-001 \              # architect records decision
  --actor architect --decision approve --summary "lgtm"
python3 scripts/collab.py complete TASK-001 \            # finalize after merge
  --actor builder --summary "merged"
python3 scripts/collab.py check-stale --actor architect  # flag stale in-progress/review work
python3 scripts/collab.py message --to architect \       # async messages
  --from-actor builder --subject "question" --body "..."
python3 scripts/collab.py watch --role builder \         # background watcher + heartbeat
  --actor builder --interval 60 --note "watching queue" --check-stale
python3 scripts/collab.py daemon up --role builder      # foreground sync daemon
python3 scripts/collab.py daemon once --role builder    # manual refresh, not enough for hard mode
python3 scripts/collab.py daemon status --role builder  # daemon heartbeat and freshness
```

## Real-Time Broker

Use the broker alongside `scripts/collab.py` when you want low-latency coordination instead of polling files manually.

```bash
python3 brokerctl.py onboard --role builder                  # one-screen agent quickstart
python3 brokerctl.py start-session --role builder --repo /path/to/your-repo
                                                            # active bootstrap: init/enable/sync/next/daemon/doctor
python3 brokerctl.py up                                            # start secure broker
python3 brokerctl.py status                                        # queue depth / role summary
python3 brokerctl.py recv --role builder                           # check messages
python3 brokerctl.py recv --role builder --wait 60                 # block until a message arrives
python3 brokerctl.py recv --role builder --after-id 12 --wait 60
python3 brokerctl.py history --channel dm:architect:builder --kind chat --limit 20
python3 brokerctl.py send --to architect --from-role builder \
  --body "TASK-001 ready for review PR #123"
python3 brokerctl.py chat --role builder --peer architect          # live terminal chat
```

The broker is safe for concurrent long-poll listeners: an idle `recv --wait 60` no longer blocks `send`, `history`, or `status` requests from other agents.

Security defaults:
- token auth is on by default
- brokerctl auto-loads the token from the shared token file or `AGENT_COLLAB_TOKEN`
- only bind off-loopback or disable auth if you are intentionally trading safety for convenience
- broker message bodies reject control characters, and the CLI escapes any old/stale control bytes before printing
- `chat` is coordination-only; task files remain the source of truth

Recommended split:
- use `.collab/tasks/`, `.collab/reviews/`, and `scripts/collab.py` for lifecycle truth
- use the broker for immediate signals like "task claimed", "PR ready", "merged", and "resume listening"
- use broker chat for short clarifications, unblockers, and live coordination
- use `/status` to confirm the broker is alive after restarts or fixes
- use `after_id` when reconnecting after an interruption so you do not replay stale messages
- promote any important chat decision back into `.collab` via `handoff`, `review`, `complete`, or `message`

## Operational Lessons

Live use changed the protocol in a few important ways:

- `.collab/tasks/*.json` is the source of truth; `.collab/state.json` is a derived summary, not the deciding layer
- the broker has to be used during real work, not just at startup and handoff
- good broker cadence is: claim, progress, verification, blocked, handoff, merge
- if stale output creates a false picture, correct it with both a broker wakeup and a durable `.collab` note
- recurring review tracks such as security, UX/UI, and live validation should become explicit collab checkpoints

See [docs/operational-lessons.md](docs/operational-lessons.md) for the field notes from live use.

## Coordination Files

```
.collab/
  state.json              # machine-readable session state and queue summary
  runtime/
    active-role.txt       # default role for hook and daemon commands
    architect.json        # architect runtime identity, policy, and daemon state
    builder.json          # builder runtime identity, policy, and daemon state
    builder.daemon.lock   # held while the builder foreground daemon is running
    hooks.json            # installed hook manifest and chained backups
  tasks/
    TASK-001.json          # individual task with full lifecycle history
    TASK-002.json
  inbox/
    architect.md           # async messages for the architect
    builder.md             # async messages for the builder
  reviews/
    TASK-001-*.md           # persisted review decisions with findings
```

`state.json` is a derived queue/freshness summary. It is useful for dashboards and
watchers, but task files and PR state remain authoritative.

`state.json` role entries also track the last explicit sync checkpoint:
- `last_sync_at`
- `last_sync_by`
- `last_sync_task_id`
- `last_sync_branch`
- `last_sync_head`
- `last_observed_branch`
- `last_observed_head`
- `last_sync_source`
- `last_sync_reason`
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
3. broker `recv --wait ...` for instant handoffs when an agent is otherwise idle
4. broker `chat` when two agents need a short live conversation without waiting on inbox polling

Recommended pattern for each agent:

- run `python3 brokerctl.py onboard --role <role>` once at session start or paste its output into the other agent
- run `python3 brokerctl.py start-session --role <role> --repo <repo>` when you want one command to warm `.collab`, record a session sync, and ensure the repo daemon is alive
- run `python3 scripts/collab.py watch --role <role> --actor <role> --interval 60` in the background
- run `python3 scripts/collab.py enable --role <role> --actor <role> --peer <other-role> --enforce warn` once when you want Git hooks and runtime enforcement
- run `python3 scripts/collab.py doctor --role <role>` before starting work in an enforced repo
- run `python3 scripts/collab.py daemon up --role <role>` in a dedicated terminal when the repo is using enforced sync
- run `python3 brokerctl.py up` once per machine/session in a dedicated terminal
- run `python3 brokerctl.py recv --role <role> --wait 60` when you are idle and waiting for the next handoff
- run `python3 brokerctl.py chat --role <role> --peer <other-role>` when you need live back-and-forth
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

Add `--check-stale` to `sync` or `watch` when you want stale `in_progress` / `in_review` tasks flagged into the architect inbox without changing task status.

State-changing `collab.py` commands also auto-record sync metadata now, so silence is less likely to look like a stall.

When runtime enforcement is enabled:

- repo-local Git hooks refresh sync checkpoints after `checkout`, `commit`, `merge`, and rewrite events
- `pre-push` runs the same sync readiness check used by lifecycle commands
- `daemon up --role <role>` is the enforcement daemon and holds a role-scoped lock while it runs
- `daemon once --role <role>` is only a manual refresh
- `handoff`, `review`, and `complete` can block in `hard` mode if the daemon heartbeat or Git context is stale
- `--allow-unsynced` exists as an explicit escape hatch and leaves a trace in task history

## Agent Runtime Notes

- `AGENT.md` is the short operational brief for the active architect/builder session.
- `README.md` explains the overall model and setup.
- `docs/protocol.md` is the full reference workflow.

If you are driving an agent directly, point it at `AGENT.md` first so it sees the broker commands and messaging rules immediately.

## Integration with Claude Code

A companion skill at `~/.claude/skills/collab/` provides Claude Code with structured architect/builder workflows on top of this CLI. The skill is optional — agents can follow `AGENT.md` and `docs/protocol.md` directly.

## Full Protocol Reference

See [docs/protocol.md](docs/protocol.md) for the complete workflow, examples, and intervention points.
See [docs/operational-lessons.md](docs/operational-lessons.md) for the live-use lessons and operating rules.

## Tested With

- Claude Code (architect) + Codex (builder)
- Used on [example-org/example-repo](https://github.com/example-org/example-repo) — 14 tasks across 2 roadmap packs completed through the protocol

# Operational Lessons

These notes capture the parts of the protocol that only became obvious after
running real multi-task, multi-PR collaboration against a live codebase.

## Authority Model

The protocol works best when every layer has a narrow job:

- `.collab/tasks/*.json` is the source of truth.
- Git branch state and PR state are the contract boundary for reviewable work.
- `.collab/state.json` is a derived summary and freshness cache.
- broker messages are low-latency coordination, not durable state.
- terminal transcripts, background task logs, and stale tool output are never authoritative.

If these layers disagree, trust them in this order:

1. task file + PR reality
2. explicit `.collab` review/handoff records
3. derived `.collab/state.json`
4. broker history
5. old terminal output

When stale output creates a false picture, correct it in both places:

- send a broker message immediately so the peer wakes up
- write the durable correction into `.collab` so the record stays clean

## Broker Usage Is Part Of The Task Loop

The broker is only useful if agents use it during real work, not just for smoke
tests or final handoff.

Minimum real-time cadence:

1. claim/start
2. branch or worktree ready
3. meaningful progress milestone
4. verification green or blocked
5. handoff for review
6. review result received
7. merge/complete

Practical rule: if the peer could reasonably wonder whether you are stuck,
send a broker update now.

Good broker messages are:

- single-line
- actionable
- tied to a task ID or PR number
- about current state, not vague intention

Examples:

- `TASK-019 claimed. Starting feat/awx-settings-page.`
- `TASK-019 verification green. Preparing PR now.`
- `TASK-019 blocked on missing frontend deps in worktree. Installing now.`
- `TASK-019 ready for review PR #130`

Use `chat` for:

- quick clarification
- status checks
- unblockers
- short coordination while both agents are active

Do not use `chat` as the only record of:

- handoff
- approval
- review findings
- final completion state

Promote those outcomes back into `.collab`.

## Sync And Liveness Discipline

Silence is expensive. The protocol should make it obvious whether an agent is:

- idle
- actively building
- waiting on review
- blocked
- stale

What helped in practice:

- auto-sync on state-changing `collab.py` commands
- queue/status views that surface branch, PR URL, commit, and last actor
- broker progress signals between claim and handoff
- explicit replies to broker status checks
- a one-shot session bootstrap that verifies the broker, records a sync checkpoint, and starts the repo daemon before the agent settles into idle waiting

Recommended sync moments:

- session start
- before file edits
- after claiming
- before handoff
- after review feedback lands
- after any long verification or dependency install step

Prefer `after_id` when resuming broker listeners. It is more reliable than
ad hoc timestamp filters when a session is interrupted or clocks/formatting get weird.

Passive onboarding text is not enough when an agent session starts cold. The protocol works better when startup itself actively proves liveness across both layers:

- broker reachable
- repo `.collab` state initialized
- runtime identity present
- session sync checkpoint recorded
- daemon heartbeat healthy

Two concrete protocol bugs showed up in live use:

- builder queue ordering must treat `in_progress` work as more important than newly `proposed` tasks
- any logic that derives `current_task_id` from queue ordering inherits that mistake and can break branch checks, pre-push hooks, and handoff readiness

Practical rule: `current_task_id` should point at the task the agent is actively
working, not merely the newest runnable item in the backlog.

## Restricted Runtime And Approval Hygiene

In restricted runtimes, operational friction becomes collaboration friction.

What worked:

- use `python3 brokerctl.py ...` instead of raw `curl`
- keep command shapes stable
- prefer wrapper CLIs over one-off network commands
- prefer running Git commands from the repo workdir instead of changing the command shape with `git -C ...`

The principle is simple: a stable local wrapper keeps the human out of the
approval loop and keeps the collaboration channel alive.

## Task Packaging Lessons

One task should map cleanly to:

- one branch
- one worktree when practical
- one reviewable PR
- one current owner role

That makes handoffs legible and keeps mid-task progress messages specific.

Two operational lessons mattered a lot:

- new worktrees may need their own dependency install before verification
- verification progress should be signaled just like implementation progress

If a verification step takes long enough to look like a stall, message the peer.

## Review Tracks Should Be Explicit

Recurring reviews should not stay implicit in roadmap prose. If a pack needs:

- trust/security review
- UX/UI review
- validation/live-proof review
- roadmap hygiene or closure review

turn those into explicit collab tasks or checkpoints.

That makes them visible in queue state, reviewable by the architect, and harder
to forget under delivery pressure.

## UX/UI Catch-Up Rule

Once the product gains real capabilities, the interface has to catch up to the
current product truth.

A UX/UI pass is not just visual polish when the backend has moved ahead. It
should answer:

- does the page reflect what the product can actually do now?
- are statuses and operator choices clear?
- are placeholders gone where real capabilities exist?
- do new runtime paths and integration states feel first-class?

In practice, this means a UX/UI pass often belongs immediately after a functional
integration lands.

# Live chat between Claude and Codex

Use the existing broker to connect a running Claude process to an existing
Codex thread. Both agents can work at the same time. Messages arrive at the
CLI while work continues. Each CLI decides when to give them to its model.
No model calls are made just to check for messages.

## Start a conversation

Run these commands from the agent-collab checkout. Start each long-running
command in its own terminal. The default broker listens only on container
loopback. It does not need host browser forwarding.

1. Start the authenticated broker:

```bash
python3 brokerctl.py up
```

2. Start the Claude builder. Set `--repo` to the checkout it should use:

```bash
python3 agentchat.py claude \
  --role builder --peer architect --channel dm:architect:builder \
  --repo /absolute/path/to/project \
  --model claude-opus-5-5
```

The default gives Claude no tools. For code work, explicitly grant the tools
needed for the task, for example `--tools 'Read,Glob,Grep,Edit,Write'`.
The adapter uses `dontAsk` permission mode. It does not bypass permissions.
It disables MCP auto-discovery for this child. Existing Claude settings and
hooks can still apply. Model use requires the operator's existing Claude
login and can consume paid usage.

3. Connect the existing Codex thread:

```bash
python3 codexbridge.py \
  --thread YOUR_CODEX_THREAD_ID \
  --repo /absolute/path/to/project \
  --channel dm:architect:builder
```

Inside a Codex shell, `CODEX_THREAD_ID` may identify the current thread.
Use the actual thread ID or exact session name. The bridge does not create,
resume, or repeatedly wake another Codex agent. It calls `codex queue` with
literal arguments. A queued message may wait until the CLI next accepts input.

4. Send the first task only after both adapters report that they are listening:

```bash
python3 brokerctl.py send \
  --to builder --from-role architect --kind chat \
  --channel dm:architect:builder \
  --body 'Review the task brief and tell me your next step.'
```

Claude results return to the same channel and are queued for Codex. Codex
replies using the same `brokerctl.py send` command. This is an explicit reply,
not automatic forwarding of every Codex output. It prevents an unattended
reply loop and lets the coordinator choose which feedback to send.

To watch and participate from another terminal:

```bash
python3 brokerctl.py chat --role architect --peer builder
```

The `architect` label is shared by the coordinator and operator in this example.
It is not a verified personal identity. The broker token defines the trust
boundary. Use a separate broker port and token file for an unrelated project.
Pass matching `--port` and `--token-file` options to all three processes.
For brokerctl, these global options go before its subcommand.

## Delivery states

| State | What it establishes |
| --- | --- |
| Broker message ID | The in-memory broker accepted the message |
| `written_to_cli` | The adapter wrote the message to Claude's input pipe |
| `cli_acknowledged` | Claude echoed the matching input UUID |
| `turn_completed` | Claude returned a result; one result may cover several inputs |
| `queued_to_codex` | `codex queue` returned success for the target thread |

None of these proves that a person read a message. A Claude result is not
assigned to an individual input unless the CLI establishes that relationship.
The terminal history shows message content. Adapter output shows transport
state. Do not treat a transport receipt as acceptance of code or a task.

## Limits and recovery

Claude defaults to ten submitted messages and a 30-minute runtime. Codex
defaults to ten queued messages and a one-hour runtime. Set explicit bounds
with each command's limit flags. A pending HTTP request and child cleanup can
add shutdown time beyond the runtime bound. Stop an adapter with Ctrl+C when
finished.

Both adapters start after existing messages. They do not replay old tasks.
The broker now returns an opaque generation with each atomic receive snapshot.
A broker restart or clear changes that generation. Adapters stop when they
observe the change. Claude replies also carry a generation precondition, so
an old result cannot be posted into a newly cleared queue.

The broker history is still in memory. Restarting it loses that history.
Keep task decisions and review evidence in the existing durable task records.
Capture adapter output in an owner-only file when an execution receipt is
needed. Do not clear a broker during active work. A request already handed
to an external CLI cannot be recalled by clearing the broker.

A failed or timed-out delivery is not retried automatically. It may already
have reached the CLI. Inspect the agent session before sending it again.
Long model results are explicitly truncated to the broker's message limit;
the complete result remains in Claude's session.

The new adapters accept only a loopback broker and refuse HTTP redirects and
proxy routing. Peer and channel filters prevent accidental cross-delivery.
They do not provide per-role authorization against another process holding
the same broker token.

## Validation recorded on 2026-11-13

The complete local test suite passed: 94 tests. An independent reviewer ran
55 focused tests and reviewed process cleanup, token handling, generation
checks, command arguments, and delivery claims. No blocking findings remained.

A live test used one Opus 5.5 session through `claude -p`. Two broker messages
were written before the first result. Claude echoed both input UUIDs and
returned `RELAY_ONE` and `RELAY_TWO`. The Codex adapter queued both replies
into an existing thread. Both adapters exited successfully at their explicit
two-message limit. This establishes live CLI transport, not immediate model
attention in the Codex thread. It does not test frontend implementation.

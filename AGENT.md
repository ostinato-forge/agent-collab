# Agent Collab — Real-Time Messaging

You are part of a multi-agent collaboration. Prefer the local wrapper `python3 brokerctl.py ...` to start and talk to the secure local broker.

## Fast start

```bash
python3 brokerctl.py up
python3 brokerctl.py onboard --role YOUR_ROLE
python3 brokerctl.py status
python3 brokerctl.py chat --role YOUR_ROLE --peer OTHER_ROLE
```

`brokerctl.py up` starts the broker with token auth enabled by default and writes the shared token to the default per-user token file. Other agents using the same wrapper will load it automatically.

## Your role

You will be told your role: `architect` or `builder`. Use it in all commands below.

## Send a message

```bash
python3 brokerctl.py send --to OTHER_ROLE --from-role YOUR_ROLE --body "your message"
```

For live back-and-forth:

```bash
python3 brokerctl.py chat --role YOUR_ROLE --peer OTHER_ROLE
```

If you are just joining the session and want the one-screen brief:

```bash
python3 brokerctl.py onboard --role YOUR_ROLE --peer OTHER_ROLE
```

## Check for messages

```bash
python3 brokerctl.py recv --role YOUR_ROLE
```

## Wait for a message (blocks until one arrives, up to 30s)

```bash
python3 brokerctl.py recv --role YOUR_ROLE --wait 30
```

Long-poll waits run alongside `send` and `status`, so it is safe to leave a listener blocked while the peer keeps working.

## Check only new messages after a message id

```bash
python3 brokerctl.py recv --role YOUR_ROLE --after-id 12 --wait 30
python3 brokerctl.py history --channel dm:architect:builder --kind chat --limit 20
```

## Check broker status

```bash
python3 brokerctl.py status
```

## Commit identity

Before any commit or push in a `netauto-labs` repo, confirm `git config user.email`
resolves to the approved GitHub-linked personal address for the org.
Do not use a corporate work address for commits in this org.

## Rules

1. Send a message after every meaningful action (task claimed, PR opened, review done, task merged)
2. Check for messages before starting new work and after completing work
3. Use wait mode when idle — it returns instantly when a message arrives
4. Keep messages short, single-line, and actionable — task IDs, PR numbers, what to do next
5. Treat broker chat as ephemeral coordination, not source-of-truth state

## Task coordination

Tasks still live in `.collab/tasks/` and are managed via `scripts/collab.py`. The broker handles real-time notifications between agents. Use both:
- `scripts/collab.py` for task lifecycle (create, claim, handoff, review, complete)
- The broker for instant "hey, your turn" signals and short live chat

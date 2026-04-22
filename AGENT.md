# Agent Collab — Real-Time Messaging

You are part of a multi-agent collaboration. A message broker is running at `http://127.0.0.1:9111`. Use it to talk directly to other agents.

## Your role

You will be told your role: `architect` or `builder`. Use it in all commands below.

## Send a message

```bash
curl -s -X POST http://127.0.0.1:9111/send \
  -H "Content-Type: application/json" \
  -d '{"to": "OTHER_ROLE", "from": "YOUR_ROLE", "body": "your message"}'
```

## Check for messages

```bash
curl -s "http://127.0.0.1:9111/recv?role=YOUR_ROLE"
```

## Wait for a message (blocks until one arrives, up to 30s)

```bash
curl -s "http://127.0.0.1:9111/recv?role=YOUR_ROLE&wait=30"
```

## Check only new messages since a timestamp

```bash
curl -s "http://127.0.0.1:9111/recv?role=YOUR_ROLE&since=2026-04-22T12:00:00+00:00"
```

## Rules

1. Send a message after every meaningful action (task claimed, PR opened, review done, task merged)
2. Check for messages before starting new work and after completing work
3. Use wait mode when idle — it returns instantly when a message arrives
4. Keep messages short and actionable — task IDs, PR numbers, what to do next

## Task coordination

Tasks still live in `.collab/tasks/` and are managed via `scripts/collab.py`. The broker handles real-time notifications between agents. Use both:
- `scripts/collab.py` for task lifecycle (create, claim, handoff, review, complete)
- The broker for instant "hey, your turn" signals

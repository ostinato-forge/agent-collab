# Response monitoring and bounded nudges

Status: implemented and independently reviewed, 2026-11-13.

## Purpose

Detect a missing response before it wastes coordination time. A queue receipt,
a running process, or unrelated model output does not prove that an agent has
acknowledged a request. Only an explicit response naming the request counts.

This extends the existing broker and live adapters. It adds no model calls for
polling, no external service, and no new dependency. Tracking remains in memory
with the broker. A restart or clear invalidates the conversation generation.
The broker retains at most 4096 tracked requests until clear and refuses new
tracked requests when full. It does not discard active requests to make room.

## Request contract

Use `send --require-response` for requests that need a reply. Ordinary chat and
signals remain untracked. Each tracked request carries:

- Its original broker message ID and conversation generation.
- Sender, recipient, and channel.
- An acknowledgement deadline, default 30 seconds.
- A progress deadline, default 120 seconds.
- A lifetime nudge budget, default two.

Deadlines start when the broker accepts the request. Start the adapters first.
The state begins as `queued`, which means broker acceptance, not model attention.

| State | Meaning |
| --- | --- |
| queued | No explicit response has arrived |
| acknowledged | The recipient explicitly accepted this message ID |
| working | The recipient reported fresh progress for this message ID |
| answered | The recipient explicitly marked its response complete |
| failed | The recipient or adapter reported a failure |
| timed_out | The response deadline and bounded nudge attempts expired |

An acknowledgement starts the progress deadline. Fresh working updates renew
that deadline. Duplicate state and detail do not renew it. Progress must say
what changed. No response renews the lifetime nudge budget. Answered, failed,
and timed-out requests are terminal; late responses do not reopen them.

## Escalation

The broker checks deadlines in its server loop. A missed deadline produces an
explicitly labeled automated nudge on the same route, referring to the original
message ID. A nudge is not a new tracked task. After the lifetime budget is used,
the broker records `timed_out` and sends an alert to the original sender.

The Claude adapter writes nudges into its existing input stream. For Codex,
`--nudge-resume` additionally requests a resume through `codex queue --resume`.
This option applies only to structured automated nudges. Queue success does
not prove that a resume succeeded. The receipt reports whether the CLI emitted a warning; it never claims a
successful wake.

A dead child process or known adapter delivery failure is reported promptly
for outstanding requests where the broker is reachable. Broker connection
failures are also visible locally. A killed adapter cannot report its own
failure; the deadline monitor remains the fallback. Neither adapter claims
that an external Codex model is responsive merely because its CLI accepted input.

## Explicit responses

For Codex or another agent with command tools:

```bash
python3 brokerctl.py respond \
  --message-id 12 --generation GENERATION_FROM_DELIVERY \
  --actor builder --state acknowledged --detail 'I will inspect the failing test.'
```

Use `working`, `answered`, or `failed` for later updates. The broker requires
the current generation, an existing tracked message, and its recipient role.
The shared broker token is the trust boundary; role labels are not separate
cryptographic identities.

For Claude, the adapter requests explicit standalone lines in model text:

```text
COLLAB_RESPONSE {"message_id":12,"state":"acknowledged","detail":"Inspecting the test"}
COLLAB_RESPONSE {"message_id":12,"state":"answered","detail":"The test failure is explained above"}
```

The adapter accepts only IDs it delivered as tracked requests in this
conversation. CLI input echoes, tool results, ordinary completion events,
quoted text, and unrelated output do not count as acknowledgements.

## Operator commands

```bash
python3 brokerctl.py send \
  --to builder --from-role architect --kind chat \
  --channel dm:architect:builder --body 'Review the failing test.' \
  --require-response --ack-timeout 30 --progress-timeout 120 --max-nudges 2

python3 brokerctl.py responses --channel dm:architect:builder
```

The timeout range is 1 to 3600 seconds. The nudge budget is 0 to 5. Zero means
alert at the first missed deadline, without a nudge. Choose progress deadlines
that fit the task. Tight deadlines can interrupt useful work or expire before
a model becomes available.

## Acceptance checks

- A queue receipt and unrelated output never acknowledge a request.
- An explicit current-generation acknowledgement changes only its named request.
- Duplicate acknowledgements do not postpone an alert.
- Fresh progress renews its deadline without resetting the nudge budget.
- Silent recipients receive only the configured number of nudges, then time out.
- Failure alerts arrive without waiting for the acknowledgement deadline.
- Clear and restart do not let late responses affect a new conversation.
- Codex resume is opt-in, literal-argument, and reported as best effort.
- Claude child exits and delivery errors do not mark completed requests failed.
- A burst larger than a receive batch does not skip older messages.

## Validation

All 157 local tests passed. Independent review covered the broker, adapters,
CLI compatibility, message identity, resets, failure handling, and bounded
escalation. No blocking findings remained.

A live Opus 5.5 session explicitly reported acknowledged, working, and answered
for one tracked request. A separate silent recipient received exactly one
configured nudge, then entered timed_out and emitted one alert. The model
adapter exited successfully at its one-turn limit.

The live test does not prove that Codex resume can wake every host session.
Its command construction, warning handling, and opt-in behavior are tested.
Codex diagnostic capture is bounded by command timeout, not a byte quota.

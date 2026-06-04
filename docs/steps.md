# steps.md — WebSocket-Only Qwen3-Omni Implementation Steps

## Loop Rule

Development must follow this loop:

```text
Reviewer reviews codebase
-> Reviewer fills tasks.md with max 10 highest-impact tasks
-> Implementer implements all active tasks
-> Tester runs safe tests and live tests when needed
-> Reviewer reads tester report
-> Reviewer creates next 10 tasks only if still necessary
```

No fake backend may be counted as production success.

RAG is removed for now.
No Qdrant/Postgres/Redis memory feature is part of this phase.

---

## Step 1 — Contract Files

Goal:

Create the required project control files.

Files:

```text
architecture.md
steps.md
agents.yaml
tasks.md
```

Acceptance:

```text
architecture.md defines WebSocket-only Qwen3-Omni architecture
steps.md defines implementation gates
agents.yaml defines Reviewer/Implementer/Tester loop
tasks.md exists
no LiveKit/WebRTC/telephony transport remains in the contract
no RAG/memory/vector/database feature remains in the contract
```

---

## Step 2 — Runtime Config

Goal:

Implement strict config validation.

Required config:

```yaml
APP_WS_HOST:
APP_WS_PORT:
VLLM_OMNI_WS_URL:
QWEN3_OMNI_MODEL:
MODEL_OUTPUT_MODE: speech
INPUT_AUDIO_SAMPLE_RATE:
OUTPUT_AUDIO_SAMPLE_RATE:
TARGET_FIRST_AUDIO_P95_MS: 500
MAX_SESSIONS:
QUEUE_MAX_AUDIO_FRAMES:
QUEUE_MAX_MODEL_EVENTS:
```

Acceptance:

```text
missing required config fails startup
invalid model URL fails startup
MODEL_OUTPUT_MODE must be speech for production pass
topology_hash is generated
failure_reason is explicit
RAG/Qdrant/Postgres/Redis config is not required
```

---

## Step 3 — Browser WebSocket Gateway

Goal:

Create browser/client WebSocket server.

Required inbound messages:

```yaml
client.session.start
client.audio.append
client.audio.commit
client.text.append
client.interrupt
client.session.close
```

Required outbound messages:

```yaml
server.ready
assistant.text.delta
assistant.audio.delta
assistant.audio.done
assistant.done
assistant.error
assistant.interrupted
metrics.partial
```

Acceptance:

```text
browser messages are validated
audio before READY is rejected
session metadata is required
input queue is bounded
no browser -> model direct bypass exists
```

---

## Step 4 — vLLM-Omni WebSocket Client

Goal:

Create app-to-model WebSocket adapter.

Required operations:

```yaml
connect
session.start
input_audio.append
input_audio.commit
input_text.append
response.create
response.cancel
session.close
reconnect_with_bounds
close
```

Acceptance:

```text
model connection failure becomes FAILED readiness
model inbound messages become kernel events
outbound messages carry lineage
no model callback mutates session state directly
response.cancel path exists
```

---

## Step 5 — RealtimeSessionKernel

Goal:

Build single application authority.

Kernel owns:

```text
session_id
turn_id
epoch
output_version
active_response_id
input_commit_state
assistant_output_state
interruption_state
cancel_state
readiness_state
dispatch_intent
replay_lineage
```

Acceptance:

```text
only enqueue_event accepts semantic events
reducer commits state
dispatch commands are collected under lock
dispatch commands execute outside lock
stale model output is rejected
```

---

## Step 6 — Audio Input Streaming

Goal:

Stream browser microphone audio into model through kernel.

Acceptance:

```text
20ms frame path exists
audio format is validated
app_receive_ns is recorded
kernel_decision_ns is recorded
input chunks are bounded
input_audio.append dispatches to model WebSocket
input_audio.commit works
```

---

## Step 7 — Model Response Streaming

Goal:

Receive Qwen3-Omni streaming response.

Acceptance:

```text
response.text.delta becomes kernel event
response.audio.delta becomes kernel event
response.audio.done becomes kernel event
response.done becomes kernel event
response.error becomes kernel event
model_first_event_ns is recorded
model_first_audio_ns is recorded
```

---

## Step 8 — Browser Audio Egress

Goal:

Stream assistant audio back to browser.

Acceptance:

```text
assistant.audio.delta is sent to browser only after lineage check
app_egress_ns is recorded
browser egress queue is bounded
audio is chunked for browser playback
stale audio is never sent
assistant.audio.done is sent correctly
```

---

## Step 9 — Browser Playback Contract

Goal:

Make browser playback compatible with realtime interruption.

Acceptance:

```text
browser can play streamed audio chunks
browser tracks epoch/output_version
browser drops stale audio
browser can stop playback immediately on assistant.interrupted
browser reports playback_start_ns when possible
browser jitter buffer target is configurable
```

---

## Step 10 — Startup and Warmup

Goal:

Make startup deterministic.

Acceptance:

```text
vLLM-Omni WebSocket connects
Qwen3-Omni model session starts
warmup response probe succeeds
speech output mode is confirmed
response.cancel probe succeeds
first packet latency is measured
system does not accept browser audio before READY
no RAG/vector/database service is required for READY
```

---

## Step 11 — Interruption and Cancellation

Goal:

Support realtime barge-in.

Acceptance:

```text
client.interrupt creates HARD_INTERRUPT
user speech while assistant active can create SOFT_PRE_INTERRUPT
response.cancel is sent to model
epoch increments
local egress stops immediately
late model chunks are dropped
old audio suppression p95 is measured
```

---

## Step 12 — Metrics and Latency Report

Goal:

Prove 500ms target.

Required timestamps:

```yaml
browser_capture_ns:
browser_send_ns:
app_receive_ns:
kernel_decision_ns:
model_ws_send_ns:
model_first_event_ns:
model_first_audio_ns:
app_egress_ns:
browser_receive_ns:
browser_playback_start_ns:
```

Acceptance:

```text
p50 first audio reported
p95 first audio reported
p99 first audio reported
sample count reported
backend readiness included
model name included
failure classification included
```

---

## Step 13 — Replay System

Goal:

Make bugs reproducible.

Acceptance:

```text
event log is written
replay rebuilds kernel state
state hash is compared
epoch/output_version are compared
dispatch intent is compared
stale output drops are compared
replay divergence fails test
```

---

## Step 14 — Drift Guards

Goal:

Prevent agents from breaking architecture.

Drift guard must catch:

```text
browser -> model direct call
model -> browser direct send
second application authority
fake READY
fake speech backend
text-only fallback counted as speech success
unbounded queues
missing cancel path
missing lineage check
LiveKit/WebRTC/telephony production dependency
RAG/vector/database dependency in realtime speech path
```

Acceptance:

```text
drift guard runs in tests/CI
forbidden pattern fails completion
test-only fixtures are allowed only under tests
```

---

## Step 15 — Live WebSocket Test Harness

Goal:

Run controlled real tests.

Tests:

```text
browser WebSocket connection
model WebSocket connection
startup readiness
warmup response
first audio latency
response.cancel
stale output suppression
browser interruption
model disconnect
reconnect behavior
replay parity
```

Acceptance:

```text
test result is PASS / FAIL / BLOCKED
failure is classified as code, config, dependency, hardware, model artifact, or network
fake backend is never counted as live pass
```

---

## Step 16 — Production Clean Gate

Goal:

Declare production-ready only with evidence.

Required:

```text
p95 first audible response <= 500ms
p99 reported
speech output mode confirmed
WebSocket cancel works
stale audio dropped
browser drops stale audio
barge-in measured
replay parity passes
drift guards pass
strict readiness works
no hidden fallback
no second authority
no LiveKit/WebRTC/telephony dependency
no RAG/vector/database realtime dependency
```

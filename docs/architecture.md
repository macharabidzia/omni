# architecture.md — Qwen3-Omni WebSocket-Only Realtime Voice Architecture

## Scope

This repository implements one production realtime voice runtime design using:

* Browser/client WebSocket transport
* App Runtime WebSocket Gateway
* `RealtimeSessionKernel` as the only application authority
* vLLM-Omni serving Qwen3-Omni
* App-to-model WebSocket streaming
* Browser audio/text output over WebSocket
* Target first audible response: `p95 <= 500ms`
* Stretch target: `p95 <= 350ms`

No LiveKit.
No WebRTC.
No telephony transport.
No Asterisk.
No SIP.
No RAG for now.
No Qdrant/Postgres/Redis memory path for now.
No worker-to-worker authority.
No direct browser-to-model bypass.

---

## Core Goal

The system must support realtime speech interaction:

```text
Browser microphone
-> Browser WebSocket
-> App Runtime WebSocket Gateway
-> RealtimeSessionKernel
-> vLLM-Omni WebSocket
-> Qwen3-Omni streaming audio/text
-> RealtimeSessionKernel lineage check
-> Browser WebSocket egress
-> Browser audio playback
```

The model server is an execution backend only.

It does not own:

* conversation authority
* cancellation policy
* interruption policy
* stale output policy
* browser playback authority
* readiness interpretation
* replay lineage
* session state
* output state

---

## Production Target

Primary latency target:

```text
first audible assistant response p95 <= 500ms
```

Stretch target:

```text
first audible assistant response p95 <= 350ms
```

Required reports:

```text
p50
p95
p99
sample_count
model readiness
WebSocket readiness
GPU/server info when available
failure classification
```

Cold model startup is not part of realtime latency.

Realtime latency is measured only after:

* app runtime is READY
* vLLM-Omni server is READY
* Qwen3-Omni is loaded
* model warmup passed
* browser WebSocket is connected
* first response probe succeeded
* speech output mode is confirmed

---

## Runtime Topology

Recommended first production topology:

```text
Process 1: app_runtime
  - Browser WebSocket server
  - RealtimeSessionKernel
  - bounded event bus
  - vLLM-Omni WebSocket client
  - interruption/cancel manager
  - browser audio egress
  - metrics reporter
  - replay logger
  - drift guards

Process 2: vllm_omni_server
  - Qwen3-Omni model
  - vLLM-Omni serving runtime
  - realtime WebSocket endpoint
  - streaming audio/text generation
```

Allowed:

* two processes
* one app authority
* app runtime connecting to model server by WebSocket
* browser connecting to app runtime by WebSocket

Forbidden:

* browser connecting directly to vLLM-Omni
* vLLM-Omni sending audio directly to browser
* second runtime authority
* extra orchestrator that overrides the kernel
* transport layer making semantic decisions
* unbounded queues
* fake backend counted as production success
* RAG/memory retrieval in the realtime path for now

---

## Canonical Live Flow

```text
1. Browser captures microphone audio.
2. Browser sends small audio frames over WebSocket.
3. App WebSocket Gateway validates session and frame metadata.
4. Gateway converts raw input into kernel event.
5. RealtimeSessionKernel.enqueue_event() accepts event.
6. Kernel reducer orders event.
7. Kernel commits state transition.
8. Kernel collects DispatchCommands.
9. Dispatch runs outside kernel lock.
10. App sends input audio/text to vLLM-Omni WebSocket.
11. Qwen3-Omni streams response text/audio.
12. Model WebSocket callback converts each response into kernel event.
13. Kernel checks epoch/output_version lineage.
14. Kernel commits assistant text/audio output if current.
15. Browser egress queue receives only valid output.
16. Browser WebSocket sends audio/text to client.
17. Browser plays audio using WebAudio/AudioWorklet.
```

---

## Single Authority Rule

Only this function may accept live semantic events:

```text
RealtimeSessionKernel.enqueue_event()
```

All of these must enter through the kernel:

* browser session start
* browser audio append
* browser audio commit
* browser text append
* browser interrupt
* browser session close
* model session ready
* model text delta
* model audio delta
* model audio done
* model response done
* model error
* model warning
* WebSocket disconnect
* WebSocket reconnect
* egress success/failure
* playback cancellation
* readiness change

Forbidden:

```text
browser -> model direct call
model -> browser direct output
gateway mutating conversation state directly
WebSocket callback mutating session state directly
transport layer deciding stale output policy
transport layer deciding cancellation policy
model server deciding browser playback state
RAG/vector/database retrieval in realtime path for now
```

---

## Kernel Ownership

`RealtimeSessionKernel` owns:

```text
session_id
turn_id
epoch
output_version
active_response_id
generation_state
input_commit_state
assistant_output_state
interruption_state
cancel_state
browser_connection_state
model_connection_state
readiness_state
dispatch_intent
replay_lineage
stale_output_suppression
```

Only kernel reducer transitions may mutate these.

---

## Lock Safety

Allowed under kernel lock:

```text
dequeue bounded events
order events
reduce events
commit state transition
collect DispatchCommands
```

Forbidden under kernel lock:

```text
await browser WebSocket send
await model WebSocket send
await model response
await audio encoding
await audio decoding
execute backend dispatch
recursive tick
recursive dispatch
database calls
vector search
RAG retrieval
```

Required pattern:

```text
lock:
  order events
  reduce events
  commit state
  collect DispatchCommands

unlock:
  execute DispatchCommands
  enqueue outputs back through RealtimeSessionKernel.enqueue_event()
```

---

## Browser WebSocket Contract

Browser sends to app:

```yaml
client.session.start
client.audio.append
client.audio.commit
client.text.append
client.interrupt
client.playback.ack
client.session.close
```

App sends to browser:

```yaml
server.ready
assistant.text.delta
assistant.audio.delta
assistant.audio.done
assistant.done
assistant.interrupted
assistant.error
metrics.partial
session.closed
```

Every JSON message must carry:

```yaml
session_id: string
turn_id: string
epoch: integer
output_version: integer
event_type: string
created_ns: integer
payload: object
```

For binary audio frames, metadata must be carried either:

* in a preceding JSON envelope
* in a compact binary header
* in a paired control message

No anonymous audio chunks are allowed in production.

---

## Browser Audio Input Contract

Preferred browser input:

```yaml
sample_rate: 16000 or 24000
frame_ms: 20
format: pcm16
channels: 1
transport: websocket_binary
```

Allowed development format:

```yaml
format: base64_pcm16
```

Production preference:

```yaml
format: binary_pcm16
```

Rules:

* browser should send small continuous chunks
* app must reject frames before READY
* app must reject frames with missing session metadata
* app must bound per-session input queue
* app must record `browser_audio_received_ns`
* app must not wait for full utterance in realtime mode

---

## Browser Audio Output Contract

Preferred output to browser:

```yaml
sample_rate: 24000 or model_native_rate
format: pcm16
frame_ms: 20
channels: 1
transport: websocket_binary
```

Browser playback:

```text
AudioWorklet preferred
ScriptProcessor forbidden for production if avoidable
small jitter buffer allowed
large playback buffer forbidden
```

Playback rules:

* browser must support immediate stop on interrupt
* browser must discard stale audio by epoch/output_version
* app must stop sending stale audio before browser playback
* browser playback buffer target: `20ms - 80ms`
* browser buffer above `120ms` must be reported as latency risk

---

## vLLM-Omni WebSocket Contract

App sends to vLLM-Omni:

```yaml
session.start
input_audio.append
input_audio.commit
input_text.append
response.create
response.cancel
session.close
```

App receives from vLLM-Omni:

```yaml
session.ready
response.text.delta
response.audio.delta
response.audio.done
response.done
response.error
server.warning
```

If vLLM-Omni protocol field names differ, adapter must translate them into this internal event contract.

The internal app contract must not change every time the model server protocol changes.

---

## Required Message Lineage

Every app-to-model message must include:

```yaml
session_id:
turn_id:
epoch:
output_version:
response_id:
created_ns:
```

Every model-to-app event must be wrapped with:

```yaml
session_id:
turn_id:
epoch:
output_version:
response_id:
received_ns:
model_event_type:
```

If model does not return lineage fields natively, the adapter must attach lineage from the active request mapping.

No model output may be emitted to browser without lineage check.

---

## Startup Contract

Startup order:

```text
1. Load runtime config.
2. Validate app WebSocket host/port.
3. Validate vLLM-Omni WebSocket URL.
4. Validate Qwen3-Omni model name/path.
5. Validate model output mode = speech.
6. Validate audio config.
7. Validate queue limits.
8. Connect to vLLM-Omni WebSocket.
9. Create warmup model session.
10. Run warmup text/audio probe.
11. Confirm streaming response event.
12. Confirm audio output mode.
13. Confirm response.cancel works.
14. Create RealtimeSessionKernel.
15. Start browser WebSocket server.
16. Mark app runtime READY.
```

Forbidden:

```text
accepting browser audio before READY
fake READY
silent text-only fallback
silent different model fallback
model download during live startup
unbounded reconnect loop
hidden WebSocket failure
production success without model warmup
production success without speech output proof
RAG dependency required for startup
database dependency required for startup
vector store dependency required for startup
```

---

## Required Config

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
BROWSER_AUDIO_FORMAT:
MODEL_AUDIO_FORMAT:
```

Optional:

```yaml
ENABLE_TEXT_DELTAS: true
ENABLE_AUDIO_DELTAS: true
BROWSER_JITTER_BUFFER_MS: 40
INTERRUPT_SUPPRESSION_TARGET_MS: 80
```

Forbidden for now:

```yaml
RAG_MODE:
QDRANT_URL:
POSTGRES_URL:
REDIS_URL:
MEMORY_RETRIEVAL_ENABLED:
VECTOR_SEARCH_ENABLED:
```

---

## Readiness Contract

Readiness states:

```yaml
WARMING
READY
DEGRADED
FAILED
```

Production speech mode requires:

```text
App WebSocket READY
AND RealtimeSessionKernel READY
AND vLLM-Omni WebSocket READY
AND Qwen3-Omni READY
AND speech output READY
AND warmup cancel probe READY
```

`DEGRADED` is not production speech success.

Text-only mode may be used for development, but cannot pass production speech-to-speech gates.

---

## Required Health Fields

```yaml
app_ws_status:
app_ws_host:
app_ws_port:
app_ws_clients:
kernel_status:
model_server_status:
model_server_url:
model_name:
model_output_mode:
model_ws_status:
model_ws_reconnects:
qwen3_omni_ready:
speech_output_ready:
warmup_first_packet_ms:
warmup_cancel_ready:
last_first_audio_ms:
p50_first_audio_ms:
p95_first_audio_ms:
p99_first_audio_ms:
active_sessions:
gpu_device_map:
topology_hash:
drift_snapshot_hash:
failure_reason:
```

Forbidden health dependency for now:

```yaml
rag_status:
qdrant_status:
postgres_status:
redis_status:
memory_status:
```

---

## 500ms Latency Budget

Target p95:

```text
browser capture frame              20-40ms
browser -> app WebSocket            5-30ms
kernel decision                     5-20ms
app -> vLLM-Omni WebSocket          5-30ms
warm model first audio/event      230-350ms
decode/resample/egress             20-60ms
browser playback buffer            20-60ms
------------------------------------------
target p95                       <= 500ms
```

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

Minimum server-side report:

```yaml
app_receive_ns:
kernel_decision_ns:
model_ws_send_ns:
model_first_event_ns:
model_first_audio_ns:
app_egress_ns:
```

Browser-side report should be added when possible.

---

## Interruption / Barge-In Contract

Interrupt levels:

```yaml
SOFT_PRE_INTERRUPT:
  evidence: browser VAD/user speech while assistant audio active
  action:
    - prepare cancellation
    - reduce browser playback buffer
    - stop enqueueing new assistant audio if confidence rises

HARD_INTERRUPT:
  evidence: browser interrupt event, committed user speech, strong VAD, or user stop command
  action:
    - send response.cancel to vLLM-Omni
    - increment epoch
    - suppress old audio locally
    - reject late model chunks
```

Required behavior:

```text
old browser audio suppression p95 <= 80ms
stretch target <= 40ms
response.cancel sent to model server
local output stops before backend confirms cancel
late model audio chunks are dropped
next user turn starts new epoch
```

The browser must also drop stale audio chunks if they arrive after interruption.

---

## Queue and Backpressure Contract

All queues must be bounded.

Required queues:

```yaml
browser_input_audio_queue:
kernel_event_queue:
model_ws_send_queue:
model_ws_recv_queue:
browser_egress_queue:
metrics_queue:
```

Required metrics:

```yaml
queue_depth:
oldest_age_ms:
drops:
stale_drops:
backpressure_action:
```

Never drop:

```yaml
client.interrupt
client.session.close
response.error
response.cancel confirmation
committed user speech
```

May drop:

```yaml
superseded VAD partials
stale audio chunks
stale text deltas
duplicate telemetry
old metrics samples
```

---

## Replay Contract

Every session must be replayable from logs.

Replay must compare:

```yaml
event_order:
epoch:
output_version:
committed_user_turns:
model_dispatch_commands:
cancel_commands:
stale_output_drops:
first_audio_timing:
final_state_hash:
```

Replay failure blocks production readiness.

---

## Drift Protection

CI/static guards must fail if production code contains:

```text
browser directly calling vLLM-Omni
vLLM-Omni directly sending to browser
second application authority
fake READY
fake speech backend
text-only fallback counted as speech success
unbounded queues
missing response.cancel path
missing stale output lineage check
startup model download
missing p95/p99 report
LiveKit/WebRTC/telephony production dependency
RAG/vector/database memory dependency in realtime path
Qdrant/Postgres/Redis required for production speech startup
```

---

## Completion Criteria

System is production-ready only when:

```text
browser WebSocket connects
vLLM-Omni WebSocket connects
Qwen3-Omni warmup passes
speech output mode is confirmed
response.cancel probe passes
browser audio is rejected until READY
all semantic events go through RealtimeSessionKernel
model output lineage is checked
stale audio is dropped before browser egress
browser also drops stale audio
first audible response p95 <= 500ms on target hardware
p99 is reported
barge-in suppression is measured
replay parity passes
drift guards pass
no fake backend is counted as production success
no LiveKit/WebRTC/telephony dependency exists
no RAG/vector/database memory dependency exists in realtime speech path
```

## Final Lock Phrase

This is the final WebSocket-only Qwen3-Omni realtime architecture contract with RAG removed for now. Future work may only be implementation, testing, observability, latency hardening, warmup hardening, cancellation hardening, replay hardening, or CI enforcement — not architecture redesign.

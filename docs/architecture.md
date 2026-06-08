# architecture.md — Qwen3-Omni LiveKit Realtime Voice Architecture

## Scope

This repository implements one production realtime voice runtime design using:

* Browser/client LiveKit WebRTC transport
* Self-hosted LiveKit server as the realtime media transport
* App Runtime LiveKit Worker / Agent
* `RealtimeSessionKernel` as the only application authority
* vLLM-Omni serving Qwen3-Omni
* App-to-model streaming adapter
* Continuous microphone audio ingest from LiveKit
* Continuous assistant audio egress to LiveKit
* Target first audible response: `p95 <= 500ms`
* Stretch target: `p95 <= 350ms`

LiveKit is the only browser realtime transport.

No browser WebSocket audio transport.
No app WebSocket gateway for browser audio.
No direct browser-to-model bypass.
No telephony transport.
No Asterisk.
No SIP.
No RAG for now.
No Qdrant/Postgres/Redis memory path for now.
No worker-to-worker authority.

---

## Core Goal

The system must support realtime speech interaction:

```text
Browser microphone
-> LiveKit WebRTC uplink
-> Self-hosted LiveKit server
-> App Runtime LiveKit Worker
-> RealtimeSessionKernel
-> vLLM-Omni streaming backend
-> Qwen3-Omni streaming audio/text
-> RealtimeSessionKernel lineage check
-> LiveKit audio track egress
-> Browser audio playback
```

LiveKit owns realtime media transport only.

LiveKit does not own:

* conversation authority
* cancellation policy
* interruption policy
* stale output policy
* semantic turn state
* readiness interpretation
* replay lineage
* assistant generation state
* model output state

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
LiveKit readiness
worker readiness
GPU/server info when available
failure classification
```

Cold model startup is not part of realtime latency.

Realtime latency is measured only after:

* app runtime is READY
* LiveKit server is reachable
* LiveKit room join succeeds
* LiveKit audio input subscription succeeds
* vLLM-Omni server is READY
* Qwen3-Omni is loaded
* model warmup passed
* first response probe succeeded
* speech output mode is confirmed

---

## Runtime Topology

Recommended first production topology:

```text
Process 1: livekit_server
  - public WebRTC signaling/media
  - ICE/STUN/TURN if enabled
  - browser room/session transport
  - audio track routing

Process 2: app_runtime_livekit_worker
  - LiveKit room participant / agent
  - audio track subscription
  - assistant audio track publication
  - RealtimeSessionKernel
  - bounded event bus
  - vLLM-Omni streaming client/adapter
  - interruption/cancel manager
  - metrics reporter
  - replay logger
  - drift guards

Process 3: vllm_omni_server
  - Qwen3-Omni model
  - vLLM-Omni serving runtime
  - streaming audio/text generation
```

Allowed:

* LiveKit as browser realtime transport
* one app authority
* app runtime connecting to LiveKit as a worker/agent
* app runtime connecting to model server through an adapter
* browser connecting only to LiveKit
* model streaming through the app runtime
* LiveKit used for continuous low-latency audio frames

Forbidden:

* browser WebSocket audio transport
* browser connecting directly to vLLM-Omni
* vLLM-Omni sending audio directly to browser
* LiveKit worker bypassing the kernel
* LiveKit callbacks mutating conversation state directly
* second runtime authority
* extra orchestrator that overrides the kernel
* transport layer making semantic decisions
* unbounded queues
* fake backend counted as production success
* RAG/memory retrieval in the realtime path for now

---

## Canonical Live Flow

```text
1. Browser joins a LiveKit room.
2. Browser publishes microphone audio track.
3. App Runtime LiveKit Worker joins the same room.
4. Worker subscribes to the user audio track.
5. Worker receives small continuous audio frames from LiveKit.
6. Worker converts audio frames into kernel events.
7. RealtimeSessionKernel.enqueue_event() accepts event.
8. Kernel reducer orders event.
9. Kernel commits state transition.
10. Kernel collects DispatchCommands.
11. Dispatch runs outside kernel lock.
12. App sends input audio/text to vLLM-Omni backend adapter.
13. Qwen3-Omni streams response text/audio.
14. Model callback converts each response into kernel event.
15. Kernel checks epoch/output_version lineage.
16. Kernel commits assistant text/audio output if current.
17. LiveKit egress queue receives only valid current audio.
18. Worker publishes assistant audio frames to LiveKit.
19. Browser receives and plays assistant audio through LiveKit/WebRTC.
```

---

## Single Authority Rule

Only this function may accept live semantic events:

```text
RealtimeSessionKernel.enqueue_event()
```

All of these must enter through the kernel:

* LiveKit room join
* LiveKit participant connected
* LiveKit participant disconnected
* LiveKit audio track subscribed
* LiveKit audio frame received
* user audio append
* user speech commit
* user text input if enabled
* user interrupt
* room/session close
* model session ready
* model text delta
* model audio delta
* model audio done
* model response done
* model error
* model warning
* LiveKit disconnect
* LiveKit reconnect
* egress success/failure
* playback cancellation
* readiness change

Forbidden:

```text
browser -> model direct call
model -> browser direct output
LiveKit callback mutating conversation state directly
LiveKit worker deciding stale output policy
LiveKit worker deciding cancellation policy
transport layer deciding interruption policy
model server deciding browser playback state
RAG/vector/database retrieval in realtime path for now
```

---

## Kernel Ownership

`RealtimeSessionKernel` owns:

```text
session_id
room_id
participant_id
turn_id
epoch
output_version
active_response_id
generation_state
input_commit_state
assistant_output_state
interruption_state
cancel_state
livekit_connection_state
livekit_room_state
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
await LiveKit audio publish
await LiveKit room operation
await model send
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

## LiveKit Transport Contract

Browser sends to app through LiveKit:

```yaml
client.audio.frame
client.speech.start
client.speech.commit
client.interrupt
client.playback.state
client.session.close
```

App sends to browser through LiveKit:

```yaml
server.ready
assistant.audio.frame
assistant.audio.done
assistant.interrupted
assistant.error
session.closed
```

Data-channel messages are allowed for control metadata only:

```yaml
server.ready
assistant.text.delta
assistant.done
assistant.error
metrics.partial
client.interrupt
client.playback.ack
session.closed
```

Audio must use LiveKit audio tracks, not browser WebSocket binary frames.

Every internal event must carry:

```yaml
session_id: string
room_id: string
participant_id: string
turn_id: string
epoch: integer
output_version: integer
event_type: string
created_ns: integer
payload: object
```

No anonymous audio frames are allowed in production.

Every audio frame received from LiveKit must be wrapped with:

```yaml
session_id:
room_id:
participant_id:
track_id:
turn_id:
epoch:
audio_frame_index:
sample_rate:
channels:
frame_ms:
received_ns:
```

---

## Browser Audio Input Contract

Preferred browser input through LiveKit:

```yaml
sample_rate: 48000
frame_ms: 10 or 20
format: livekit_audio_frame
channels: 1
transport: livekit_webrtc
```

Worker internal normalized input:

```yaml
sample_rate: 16000 or 24000
frame_ms: 20
format: pcm16
channels: 1
```

Rules:

* browser publishes microphone as a LiveKit audio track
* app must reject frames before READY
* app must reject frames with missing session metadata
* app must bound per-session input queue
* app must record `livekit_audio_received_ns`
* app must not wait for full utterance in realtime mode
* resampling must happen outside kernel lock
* LiveKit callback must not mutate semantic state directly

---

## Browser Audio Output Contract

Preferred assistant output through LiveKit:

```yaml
sample_rate: 48000
format: livekit_audio_frame
frame_ms: 10 or 20
channels: 1
transport: livekit_webrtc
```

Model-native output may be:

```yaml
sample_rate: 24000
format: pcm16
channels: 1
```

Worker must publish LiveKit-compatible PCM audio and only resample model-native audio when the model output rate differs from the configured LiveKit output rate.

Playback rules:

* browser must support immediate stop on interrupt
* app must stop publishing stale assistant audio before browser playback
* assistant audio must be tagged by epoch/output_version internally
* stale audio must be dropped before LiveKit egress
* browser-side stale-drop by data-channel metadata is allowed as extra protection
* LiveKit egress buffer target: `20ms - 80ms`
* egress buffer above `120ms` must be reported as latency risk

---

## LiveKit Worker Contract

The app runtime must connect to LiveKit using:

```yaml
LIVEKIT_URL:
LIVEKIT_API_KEY:
LIVEKIT_API_SECRET:
LIVEKIT_ROOM:
LIVEKIT_AGENT_ID:
LIVEKIT_INPUT_SAMPLE_RATE:
LIVEKIT_OUTPUT_SAMPLE_RATE:
LIVEKIT_OUTPUT_FRAME_MS:
LIVEKIT_OUTPUT_QUEUE_MS:
```

Worker responsibilities:

```text
connect to LiveKit
join target room
subscribe to user microphone track
normalize inbound audio frames
enqueue audio events into RealtimeSessionKernel
publish assistant audio track
send control messages through LiveKit data channel if needed
report connection/readiness metrics
handle reconnects without state corruption
```

Worker must not:

```text
decide conversation state
decide stale output validity
decide cancellation policy
call model directly without kernel dispatch
publish model output before lineage check
hold unbounded audio buffers
count LiveKit connection alone as READY
```

---

## Model Streaming Adapter Contract

App sends to model adapter:

```yaml
session.start
input_audio.append
input_audio.commit
input_text.append
response.create
response.cancel
session.close
```

App receives from model adapter:

```yaml
session.ready
response.text.delta
response.audio.delta
response.audio.done
response.done
response.error
server.warning
```

If the model server protocol field names differ, adapter must translate them into this internal event contract.

The internal app contract must not change every time the model server protocol changes.

The model transport may be WebSocket, HTTP streaming, gRPC, or local process IPC, but it is not the browser/client transport and must remain behind the kernel authority.

---

## Required Message Lineage

Every app-to-model message must include:

```yaml
session_id:
room_id:
participant_id:
turn_id:
epoch:
output_version:
response_id:
created_ns:
```

Every model-to-app event must be wrapped with:

```yaml
session_id:
room_id:
participant_id:
turn_id:
epoch:
output_version:
response_id:
received_ns:
model_event_type:
```

If model does not return lineage fields natively, the adapter must attach lineage from the active request mapping.

No model output may be emitted to LiveKit without lineage check.

---

## Startup Contract

Startup order:

```text
1. Load runtime config.
2. Validate LiveKit URL.
3. Validate LiveKit API key and secret.
4. Validate LiveKit room/agent settings.
5. Validate model backend URL/path.
6. Validate Qwen3-Omni model name/path.
7. Validate model output mode = speech.
8. Validate audio config.
9. Validate queue limits.
10. Connect to model backend.
11. Create warmup model session.
12. Run warmup text/audio probe.
13. Confirm streaming response event.
14. Confirm audio output mode.
15. Confirm response.cancel works.
16. Connect LiveKit worker.
17. Join LiveKit room.
18. Confirm audio track publish capability.
19. Confirm audio track subscribe capability.
20. Create RealtimeSessionKernel.
21. Mark app runtime READY.
```

Forbidden:

```text
accepting LiveKit user audio before READY
fake READY
silent text-only fallback
silent different model fallback
model download during live startup
unbounded reconnect loop
hidden LiveKit failure
production success without model warmup
production success without speech output proof
production success without LiveKit join proof
RAG dependency required for startup
database dependency required for startup
vector store dependency required for startup
```

---

## Required Config

```yaml
LIVEKIT_URL:
LIVEKIT_API_KEY:
LIVEKIT_API_SECRET:
LIVEKIT_ROOM:
LIVEKIT_AGENT_ID:
MODEL_BACKEND_URL:
QWEN3_OMNI_MODEL:
MODEL_OUTPUT_MODE: speech
QWEN_AUDIO_INPUT_SAMPLE_RATE: 16000
QWEN_AUDIO_OUTPUT_SAMPLE_RATE: 24000
LIVEKIT_INPUT_SAMPLE_RATE: 48000
LIVEKIT_OUTPUT_SAMPLE_RATE: 48000
LIVEKIT_OUTPUT_FRAME_MS: 20
LIVEKIT_OUTPUT_QUEUE_MS: 40
LIVEKIT_PREROLL_FRAMES: 1
TARGET_FIRST_AUDIO_P95_MS: 500
MAX_SESSIONS:
QUEUE_MAX_AUDIO_FRAMES:
QUEUE_MAX_MODEL_EVENTS:
QUEUE_MAX_LIVEKIT_EGRESS_FRAMES:
MODEL_AUDIO_FORMAT:
```

Optional:

```yaml
ENABLE_TEXT_DELTAS: true
ENABLE_AUDIO_DELTAS: true
LIVEKIT_DATA_CHANNEL_CONTROL: true
LIVEKIT_EGRESS_JITTER_BUFFER_MS: 40
INTERRUPT_SUPPRESSION_TARGET_MS: 80
```

Forbidden for now:

```yaml
APP_WS_HOST:
APP_WS_PORT:
BROWSER_WEBSOCKET_URL:
BROWSER_AUDIO_FORMAT: websocket_binary
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
LiveKit server reachable
AND LiveKit worker connected
AND LiveKit room joined
AND LiveKit input track subscription ready
AND LiveKit assistant audio publishing ready
AND RealtimeSessionKernel READY
AND model backend READY
AND Qwen3-Omni READY
AND speech output READY
AND warmup cancel probe READY
```

`DEGRADED` is not production speech success.

Text-only mode may be used for development, but cannot pass production speech-to-speech gates.

---

## Required Health Fields

```yaml
livekit_status:
livekit_url:
livekit_room:
livekit_worker_status:
livekit_room_joined:
livekit_input_track_status:
livekit_output_track_status:
livekit_reconnects:
livekit_rtt_ms:
livekit_packet_loss:
livekit_egress_queue_depth:
kernel_status:
model_server_status:
model_server_url:
model_name:
model_output_mode:
model_reconnects:
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
app_ws_status:
app_ws_host:
app_ws_port:
browser_ws_clients:
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
browser capture frame              10-40ms
browser -> LiveKit server           5-30ms
LiveKit -> worker                   5-30ms
kernel decision                     5-20ms
worker -> model backend             5-30ms
warm model first audio/event      230-350ms
decode/resample/LiveKit egress      20-60ms
browser playback                    20-60ms
------------------------------------------
target p95                       <= 500ms
```

Required timestamps:

```yaml
browser_capture_ns:
livekit_uplink_send_ns:
livekit_worker_receive_ns:
kernel_decision_ns:
model_send_ns:
model_first_event_ns:
model_first_audio_ns:
livekit_egress_ns:
browser_receive_ns:
browser_playback_start_ns:
```

Minimum server-side report:

```yaml
livekit_worker_receive_ns:
kernel_decision_ns:
model_send_ns:
model_first_event_ns:
model_first_audio_ns:
livekit_egress_ns:
```

Browser-side report should be added when possible.

---

## Interruption / Barge-In Contract

Interrupt levels:

```yaml
SOFT_PRE_INTERRUPT:
  evidence: LiveKit user speech/VAD while assistant audio active
  action:
    - prepare cancellation
    - reduce LiveKit egress buffer
    - stop enqueueing new assistant audio if confidence rises

HARD_INTERRUPT:
  evidence: explicit client interrupt, committed user speech, strong VAD, or user stop command
  action:
    - send response.cancel to model backend
    - increment epoch
    - suppress old audio locally
    - stop publishing stale assistant audio to LiveKit
    - reject late model chunks
```

Required behavior:

```text
old assistant audio suppression p95 <= 80ms
stretch target <= 40ms
response.cancel sent to model server
local output stops before backend confirms cancel
late model audio chunks are dropped
next user turn starts new epoch
```

The browser may also drop stale audio if it receives stale metadata through LiveKit data channel.

---

## Queue and Backpressure Contract

All queues must be bounded.

Required queues:

```yaml
livekit_input_audio_queue:
kernel_event_queue:
model_send_queue:
model_recv_queue:
livekit_egress_queue:
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
LiveKit disconnect
LiveKit reconnect
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
livekit_egress_timing:
final_state_hash:
```

Replay failure blocks production readiness.

---

## Drift Protection

CI/static guards must fail if production code contains:

```text
browser WebSocket audio transport
browser directly calling vLLM-Omni
vLLM-Omni directly sending to browser
LiveKit callbacks mutating semantic state directly
LiveKit worker bypassing RealtimeSessionKernel
second application authority
fake READY
fake speech backend
text-only fallback counted as speech success
unbounded queues
missing response.cancel path
missing stale output lineage check
startup model download
missing p95/p99 report
telephony production dependency
Asterisk production dependency
SIP production dependency
RAG/vector/database memory dependency in realtime path
Qdrant/Postgres/Redis required for production speech startup
```

---

## Completion Criteria

System is production-ready only when:

```text
LiveKit server is reachable
LiveKit worker joins room
browser publishes microphone track
worker subscribes to browser audio
worker publishes assistant audio track
model backend connects
Qwen3-Omni warmup passes
speech output mode is confirmed
response.cancel probe passes
LiveKit user audio is rejected until READY
all semantic events go through RealtimeSessionKernel
model output lineage is checked
stale audio is dropped before LiveKit egress
first audible response p95 <= 500ms on target hardware
p99 is reported
barge-in suppression is measured
replay parity passes
drift guards pass
no fake backend is counted as production success
no browser WebSocket audio transport exists
no telephony dependency exists
no RAG/vector/database memory dependency exists in realtime speech path
```

## Final Lock Phrase

This is the final LiveKit-based Qwen3-Omni realtime architecture contract with RAG removed for now. Future work may only be implementation, testing, observability, latency hardening, warmup hardening, cancellation hardening, replay hardening, LiveKit transport hardening, or CI enforcement — not architecture redesign.

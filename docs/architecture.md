# architecture.md — Final clean architecture for continuous smooth audio

**Scope:** production-ready low-latency voice path for the current `apps/gateway` design.

**Principle:** keep the architecture direct. Audio should travel as WebRTC media through LiveKit and as streaming PCM through Qwen realtime. HTTP, JSON, metrics, and diagnostics must not sit in the hot audio path.

---

## Goals

- Warm p95 `speech_end_to_first_assistant_egress_ms <= 500 ms`.
- Smooth continuous assistant audio with stable frame cadence, no avoidable queue buildup, no clicks/pops, and safe interruption.
- Clear ownership of each concern: API tokens, room worker, turn lifecycle, Qwen session, audio publishing, metrics.
- Minimal moving parts: no broker, no parallel audio transport, no duplicated VAD ownership, no extra orchestration layer.

## Non-goals

- Multi-speaker conversation inside one room.
- Browser-owned turn commits while server VAD is enabled.
- Sending realtime audio over JSON/base64 to the browser.
- Per-frame persistent storage, tracing, or database writes.
- Replacing LiveKit’s media transport with custom WebRTC or raw websocket audio.

---

## Target latency budget

This budget is for the warm path after the browser has joined, the assistant track is published/subscribed, and Qwen is ready.

| Segment | Target p95 | Notes |
|---|---:|---|
| Last user audio frame accepted → VAD end | 120–180 ms | Controlled by VAD min silence and frame cadence. |
| VAD end → Qwen commit sent | <= 20 ms | No full utterance re-upload or blocking work. |
| Qwen commit sent → first Qwen audio delta | <= 250–320 ms | Main model/runtime budget. |
| First Qwen audio delta → first LiveKit assistant frame | <= 30 ms | Decode/resample/frame/publish overhead. |
| LiveKit/browser jitter/playout | track separately | Browser/network adds variable delay outside Python. |

**Primary SLO:** `speech_end_to_first_assistant_egress_ms <= 500 ms p95`.

**Important distinction:** server egress and browser-heard audio are not the same metric. The server can be under 500 ms while the browser adds jitter-buffer or output-device delay. Track both.

---

## High-level system diagram

```text
Browser
  ├─ gets token from FastAPI /livekit/session
  ├─ publishes microphone as LiveKit audio track
  ├─ subscribes to assistant LiveKit audio track
  └─ sends/receives small control + telemetry packets
        │
        ▼
LiveKit SFU / Room
        │
        ▼
Gateway LiveKit Worker
  ├─ owns one active participant session per room
  ├─ consumes browser audio at Qwen input rate
  ├─ runs server-side VAD and turn lifecycle
  ├─ streams PCM16 chunks to Qwen realtime
  ├─ receives assistant text/audio events
  ├─ filters stale output by output_version
  └─ publishes assistant audio frames through AudioSource
        │
        ▼
Qwen Realtime Backend
  ├─ warm websocket per active session
  ├─ accepts PCM16 mono 16 kHz input chunks
  └─ streams incremental assistant audio deltas
```

FastAPI is intentionally outside the media loop. It creates sessions/tokens and reports health; it does not proxy turn audio.

---

## Runtime services

### 1. API service: `apps/gateway/src/main.py`

Responsibilities:

- Create the FastAPI app.
- Configure logging and CORS.
- Mount health/readiness routes.
- Mount LiveKit token/session route.

Rules:

- Stateless.
- Horizontally scalable.
- No LiveKit room state.
- No Qwen websocket state.
- No audio frame processing.

### 2. LiveKit worker service: `apps/gateway/src/livekit/worker.py`

Responsibilities:

- Connect as the assistant/agent participant.
- Publish the assistant audio track.
- Accept only one active browser participant per room.
- Subscribe to the active browser microphone track.
- Own `ParticipantBridgeSession` for the active participant.
- Convert LiveKit audio frames into Qwen input chunks.
- Run server-side VAD and turn state.
- Handle interruption and stale-output suppression.
- Publish control/data events back to the browser.

Rules:

- One active participant per worker room in the current architecture.
- Do not put this inside multi-worker FastAPI.
- All hot-path queues must be bounded.
- Enforce a hard max turn duration and drop overrun tail audio until the speech boundary closes.
- No per-frame logs in production.

### 3. Realtime session: `apps/gateway/src/realtime/session.py`

Responsibilities:

- Own one Qwen conversation/session bridge.
- Open and keep a warm Qwen realtime websocket.
- Append user audio as it arrives.
- Commit on VAD end.
- Gate assistant output until commit when needed.
- Restart/recover on transient upstream send failures.
- Cancel/restart on hard interrupt.
- Emit normalized runtime events.
- Maintain metrics and output version.

Rules:

- Do not reconnect per turn unless recovery/restart requires it.
- Do not use chat-completion fallback in the production realtime audio path.
- Do not leak raw upstream event shapes to the browser.
- `output_version` must guard all assistant output.

### 4. Qwen realtime client: `apps/gateway/src/realtime/qwen_client.py`

Responsibilities:

- Open websocket and validate startup event.
- Send `session.update`.
- Start input stream before first append.
- Append PCM16 base64 chunks.
- Commit audio.
- Normalize transcript, text, audio, done, and error events.
- Detect retryable closed/unreachable cases.

Rules:

- Input is PCM16 mono 16 kHz for realtime.
- Audio output sample rate comes from upstream event metadata when available.
- The client should not know about LiveKit.

### 5. Assistant audio publisher: `apps/gateway/src/livekit/output.py`

Responsibilities:

- Decode assistant audio chunks.
- Convert model output rate to LiveKit publish rate.
- Maintain streaming resampler state across chunks.
- Trim safe leading/inter-chunk silence.
- Preserve natural short pauses.
- Smooth boundary jumps and isolated spikes.
- Accumulate complete LiveKit frames.
- Publish frames to `rtc.AudioSource.capture_frame`.
- Expose queued duration and clear/wait/finalize operations.

Rules:

- This is the only output audio adapter.
- Resample once.
- Frame once.
- Flush resampler only at turn finalization.
- Queue depth is a latency signal and must be bounded.

### 6. Audio helpers: `apps/gateway/src/realtime/audio.py`

Responsibilities:

- Validate chunk format/duration.
- Convert byte counts to durations.
- Provide PCM16 NumPy views/helpers.
- Convert WAV only for fallback/offline paths.
- Split PCM16 into fixed-duration chunks.

Rules:

- Realtime path should avoid WAV wrapping.
- Use copy-free views where safe.

### 7. Event map: `apps/gateway/src/realtime/event_map.py`

Responsibilities:

- Convert internal Qwen events into public gateway events.
- Keep browser event schema stable.

Rules:

- Public events are small.
- Audio goes to browser as LiveKit media, not as public JSON in production.

### 8. Metrics: `apps/gateway/src/realtime/metrics.py`

Responsibilities:

- Record turn/session milestones.
- Emit relative timestamps and derived latencies.
- Provide one turn summary plus key update events.

Rules:

- Metrics must be cheap.
- Do not emit every frame.
- All metric names use milliseconds.

---

## Audio rate and frame contract

| Direction | Format | Rate | Owner |
|---|---|---:|---|
| Browser mic → LiveKit | WebRTC audio track | browser-native/WebRTC | Browser + LiveKit |
| LiveKit worker input stream → Qwen | PCM16 mono | 16 kHz | Worker audio stream |
| Qwen realtime input chunks | base64 PCM16 mono | 16 kHz | `RealtimeSession` / `QwenRealtimeClient` |
| Qwen realtime output | base64 PCM16 | model native, typically 24 kHz | Qwen backend |
| Gateway assistant publish | PCM16 mono frames | 48 kHz | `AssistantAudioPublisher` |
| Browser playback | WebRTC audio track | browser/device | LiveKit + browser |

Production defaults:

- Qwen input chunk target: 80 ms to start; benchmark 40 ms only if needed.
- LiveKit output frame target: 10 ms.
- LiveKit assistant audio source queue target: 150–200 ms.
- Qwen websocket: open on `session.start`, keep warm across turns.
- RED: enabled for assistant publish.
- DTX: disabled for assistant TTS.

---

## Turn lifecycle

```text
idle
  └─ VAD speech_start
       ▼
user_speaking
  ├─ flush preroll to Qwen
  ├─ stream live user audio to Qwen
  └─ VAD speech_end
       ▼
committing
  ├─ send final input commit
  └─ open output gate
       ▼
assistant_streaming
  ├─ receive Qwen transcript/text/audio
  ├─ publish first assistant audio as soon as possible
  ├─ buffer or order UI/control events so audio is not delayed
  └─ response done / finalized
       ▼
idle
```

Invalid duplicate transitions are ignored with a structured reason.

---

## Interrupt lifecycle

```text
assistant_streaming or queued_assistant_audio
  └─ new VAD speech_start or browser interrupt
       ├─ clear LiveKit AudioSource queue
       ├─ clear AssistantAudioPublisher buffers
       ├─ cancel/restart Qwen realtime session if required
       ├─ increment output_version
       ├─ publish assistant.interrupted control event
       └─ start new user turn with preroll
```

Hard rules:

- Any assistant output with an old `output_version` is dropped.
- Queue clear must happen before new assistant audio can be published.
- New user speech wins over old assistant playout.

---

## Queue and backpressure policy

| Queue/buffer | Target | Action when unhealthy |
|---|---:|---|
| Browser/WebRTC jitter | observe only | surface via browser stats |
| Worker preroll | 200–300 ms | cap/drop oldest silence if exceeded |
| VAD event queue | small bounded | log state warning, drop duplicate events safely |
| Qwen pending event buffer | bounded by turn | drop stale output by version |
| Pending pre-audio UI events | tiny | flush at first audio or done |
| Assistant audio source queue | 150–200 ms target | warn near cap, clear on interrupt, and hard-drop overloaded pending frames once the queue reaches the configured cap |

Backpressure should be visible in metrics. It should not silently accumulate.
Per-turn replay audio and pre-audio payload buffers must be bounded by the same max-turn/resource limits as live speech.

---

## Public event schema

Minimum public events:

```text
session.ready
turn.started
turn.committed
transcript.delta
assistant.audio_started
assistant.done
assistant.interrupted
metrics.update
error
room.busy
```

Every event should include:

```text
session_id
participant_identity
room
turn_id
output_version
backend
timestamp_ms
```

Rules:

- Critical control events are reliable.
- Frequent telemetry is best-effort/debug-only.
- Internal upstream/Qwen payload shapes are not public API.
- `metrics.update` may include per-turn metrics, worker rollups, reconnect reasons, and worker counters such as interrupts, duplicate commits, stale drops, and error totals.

---

## Observability model

### Required per-turn timestamps

```text
session_start_received
qwen_ws_connected
qwen_session_ready
vad_speech_start
first_user_audio_uploaded
last_user_audio_uploaded
vad_speech_end
commit_sent
qwen_first_transcript
qwen_first_text
qwen_first_audio
first_livekit_frame_captured
assistant_done
turn_closed
```

### Required derived metrics

```text
vad_end_to_commit_ms
commit_to_qwen_first_audio_ms
qwen_first_audio_to_livekit_first_frame_ms
speech_end_to_first_assistant_egress_ms
speech_start_to_commit_ms
turn_total_ms
queue_depth_at_first_frame_ms
browser_jitter_buffer_ms
```

### Minimum dashboard

- p50/p95/p99 `speech_end_to_first_assistant_egress_ms`.
- p50/p95 `commit_to_qwen_first_audio_ms`.
- p95 `qwen_first_audio_to_livekit_first_frame_ms`.
- Assistant queue depth.
- Reconnects by reason.
- Error count.
- Interrupts.
- Duplicate commits ignored.
- Stale outputs dropped.
- Browser jitter buffer estimate.
- Error counts by code when available.

---

## Deployment architecture

```text
[Browser]
   │
   ├── HTTPS token request ───────────────► [FastAPI API service]
   │                                             │
   │                                             └── health/readiness/diagnostics
   │
   └── WebRTC media/data ────────────────► [LiveKit]
                                                │
                                                ▼
                                      [Gateway LiveKit Worker]
                                                │
                                                ▼
                                      [Qwen Realtime Backend]
```

Deployment rules:

- Keep API and worker as separate services/processes.
- Keep worker and Qwen in the same region/network zone where possible.
- Keep browser connected to the nearest sensible LiveKit region.
- Scale API statelessly.
- Scale workers by active rooms/sessions.
- Track Qwen/GPU capacity separately from API capacity.
- Use graceful drain on deploys.

---

## Graceful drain

Drain is the only supported deploy/restart path for the LiveKit worker.

1. Mark the worker snapshot as `draining`.
2. Return degraded `/ready` and reject new `/livekit/session` requests.
3. Reject new browser participants/tracks with `room.busy`.
4. Let the active participant finish naturally when possible.
5. After `LIVEKIT_DRAIN_TIMEOUT_SECONDS`, force-close active sessions, clear assistant playout state, and disconnect.

Rules:

- Drain is visible in readiness and worker snapshot state.
- Drain must not accept a new browser session while the old one is winding down.
- Forced drain is better than leaving a ghost assistant track or orphaned warm Qwen session.

---

## Health and readiness

- `/health`: process is alive.
- `/ready`: shallow dependency readiness; cheap enough for normal orchestration.
- `/ready?deep=true` or diagnostics route: Qwen websocket startup and optional inference probe; TTL-cached.
- LiveKit probe: config, room visibility, worker presence, assistant track publication.
- Worker snapshot augments readiness with reconnect counts, active session count, queue depth, and available p50/p95/p99 first-audio rollups.
- Qwen probe: HTTP/health if available, websocket startup, optional audio inference.

Do not run expensive inference probes on every orchestrator health check.

---

## Retry and timeout policy

| Condition | Classification | Runtime action | Browser-visible result |
|---|---|---|---|
| Realtime websocket startup/connect fails with retryable closed/refused/timeout error | retryable | Retry reopen within `QWEN_REQUEST_TIMEOUT_SECONDS` | If retries exhaust, `error` with `QWEN_UNAVAILABLE` |
| Realtime append send fails and buffered turn audio is available | retryable | Restart realtime session, replay buffered turn audio, continue turn | No user-visible error if recovery succeeds |
| Realtime commit send fails and buffered turn audio is available | retryable | Restart realtime session, replay buffered turn audio, re-commit | No user-visible error if recovery succeeds |
| Realtime append/commit fails with non-retryable error | terminal | Close broken session path | `error` with `QWEN_APPEND_FAILED` or `QWEN_COMMIT_FAILED` |
| Upstream websocket closes before terminal response event | retryable-on-next-open | Mark `upstream_closed`, drop stale output, reopen when recovery or next turn requires it | `error` with `QWEN_CONNECTION_CLOSED` |
| First assistant audio does not arrive by `QWEN_FIRST_AUDIO_TIMEOUT_SECONDS` | terminal-for-current-response | Emit structured error, mark turn closed, bump `output_version`, restart realtime session | `error` with `QWEN_FIRST_AUDIO_TIMEOUT` |
| Assistant response does not finish by `QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS` | terminal-for-current-response | Emit structured error, mark turn closed, bump `output_version`, restart realtime session | `error` with `QWEN_RESPONSE_TIMEOUT` |
| Browser session stays idle past `LIVEKIT_BROWSER_IDLE_TIMEOUT_SECONDS` | terminal-for-idle-session | Emit structured error, close session state, clear worker-side audio/VAD/runtime state | `error` with `BROWSER_IDLE_TIMEOUT` |
| User barge-in or manual interrupt while assistant output is active | terminal-for-old-response | Clear playout, close upstream socket, restart realtime session | `assistant.interrupted` |
| Worker drain timeout reached | terminal-for-active-session | Close active participant sessions and stop worker | New joins rejected until worker replacement is ready |

Rules:

- Retryable classification is code, not guesswork; `is_qwen_connection_retryable_error()` is the source of truth.
- Hard interrupt means kill the old output path. Do not depend on polite upstream cancel semantics.
- Timeout recovery must always preserve stale-output safety by advancing `output_version` before new assistant egress.
- Browser idle timeout must be long enough to tolerate normal conversation pauses while still reclaiming abandoned sessions.

---

## Testing architecture

### Unit tests

Keep and expand current tests for:

- Audio validation and chunking.
- Settings rate/queue validation.
- AssistantAudioPublisher resampling, trimming, smoothing, queue warning/cap.
- Worker turn lifecycle, VAD, duplicate commit, interrupt, stale output.
- Qwen event parsing and connection errors.
- Session output gating, retry/restart recovery, fallback modes.
- Qwen initial codec chunk behavior.
- Audio artifact capture only when explicitly enabled for debug/perf runs.

### Integration tests

Add:

- Qwen websocket stub that emits first audio after configurable delay.
- Local LiveKit worker test for first egress timing.
- Browser smoke test for join, subscribe, first playout, interrupt.
- Audio capture analyzer against a golden assistant-output WAV.

### Performance tests

Add:

- `scripts/benchmark_qwen_ttfb.py` for warm Qwen append/commit/first-audio timing. It defaults to a stub websocket and can target a live realtime URL when available.
- `scripts/benchmark_livekit_egress.py` for gateway-only `AssistantAudioPublisher` egress timing with threshold gating on first `capture_frame`.
- `scripts/benchmark_realtime_turn.py` for the in-process warm turn path using the real worker/session/publisher stack against a stub realtime backend.
- Full warm browser→LiveKit→worker→Qwen→LiveKit benchmark when a live backend and browser harness are available.
- Cold-start benchmark separately; do not mix it into warm p95 SLO.

---

## Configuration contract

Required production settings should be documented with safe defaults:

```text
QWEN_REALTIME_URL
QWEN_MODEL
QWEN_INPUT_SAMPLE_RATE_HZ=16000
QWEN_OUTPUT_SAMPLE_RATE_HZ=24000
QWEN_AUDIO_BACKEND=realtime
QWEN_REQUEST_TIMEOUT_SECONDS
QWEN_RESPONSE_TIMEOUT_SECONDS
QWEN_FIRST_AUDIO_TIMEOUT_SECONDS
QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS
QWEN_PREWARM_ON_STARTUP
QWEN_PREWARM_MAX_WAIT_SECONDS
QWEN_PREWARM_RETRY_INTERVAL_SECONDS
QWEN_TEXT_MAX_COMPLETION_TOKENS=<fallback text cap>
voice response token cap: configured in the deployed Qwen runtime (default local configs keep stage-0 `max_tokens: 16`)
LIVEKIT_URL
LIVEKIT_API_KEY
LIVEKIT_API_SECRET
LIVEKIT_ROOM
LIVEKIT_AGENT_IDENTITY
LIVEKIT_BROWSER_IDENTITY_PREFIX
LIVEKIT_INPUT_SAMPLE_RATE_HZ=48000
LIVEKIT_PUBLISH_SAMPLE_RATE_HZ=48000
LIVEKIT_OUTPUT_FRAME_MS=10
LIVEKIT_AUDIO_SOURCE_QUEUE_MS=150-200
LIVEKIT_DRAIN_TIMEOUT_SECONDS
LIVEKIT_BROWSER_IDLE_TIMEOUT_SECONDS
LIVEKIT_ASSISTANT_RED=true
LIVEKIT_ASSISTANT_DTX=false
VAD_ENABLED=true
VAD_MIN_SPEECH_MS=80-160
VAD_MIN_SILENCE_MS=120-180
VAD_PREROLL_MS=200-300
DEBUG_AUDIO_ARTIFACTS=false
DEBUG_RAW_QWEN_EVENTS=false
```

Startup must fail fast on unsupported or unsafe values.

---

## Clean module boundaries

```text
config.py
  Settings only. No runtime audio logic.

health.py
  Liveness/readiness/diagnostics only. No participant state.

livekit/auth.py
  Identity/token grants only.

livekit/control.py
  Browser session/token endpoint only.

livekit/worker.py
  Room connection, participant session, VAD ownership, turn lifecycle, control packets.

livekit/output.py
  Assistant audio decode/resample/smooth/frame/publish only.

realtime/audio.py
  PCM16/WAV/chunk helper functions only.

realtime/qwen_client.py
  Qwen websocket protocol only.

realtime/qwen_chat_client.py
  Fallback chat/audio streaming only.

realtime/session.py
  Conversation runtime orchestration only.

realtime/event_map.py
  Internal event → public event conversion only.

realtime/metrics.py
  Timestamps and derived latency metrics only.
```

Rule: if a function needs LiveKit and Qwen and VAD together, it belongs in `ParticipantBridgeSession` or `RealtimeSession`. If it only touches PCM bytes, it belongs in audio/output helpers.

---

## Final “clean path” checklist

A turn is healthy when this exact sequence happens:

1. Browser has already joined LiveKit and subscribed to assistant audio.
2. Browser publishes mic track.
3. Worker consumes mic frames as 16 kHz mono.
4. VAD start fires.
5. Worker starts turn and flushes preroll.
6. User audio streams to Qwen before VAD end.
7. VAD end fires.
8. Worker commits immediately.
9. Qwen streams first assistant audio delta.
10. `AssistantAudioPublisher` decodes, resamples, frames, and publishes immediately.
11. First LiveKit frame is captured and metrics mark assistant egress.
12. Browser plays assistant track.
13. On interruption, queue clears, Qwen cancels/restarts, `output_version` increments, stale output drops.
14. On done, publisher finalizes the turn and resets per-turn smoothing state.

If any step is slow, the timestamp timeline should show exactly where.


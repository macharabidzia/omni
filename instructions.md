# BUILD_INSTRUCTIONS.md — Instructions For AI Builder

## Role

You are the implementation agent for a fresh pure Qwen3-Omni native realtime voice project.

Your job is to build the project from scratch according to `ARCHITECTURE.md`.

You must implement real code, real Docker services, real WebSocket streaming, real browser microphone capture, real assistant audio playback, and real metrics.

Do not import or reuse any old voice pipeline code.

---

## Core Rule

This project must use Qwen3-Omni natively.

Do not add separate ASR or TTS systems.

Forbidden:

```text
Vosk
Whisper
CosyVoice
Piper
Kokoro
ElevenLabs
LiveKit dependency for first version
old project runtime
mock model server
fake streaming
full-audio-only playback
```

Allowed:

```text
Qwen3-Omni
vLLM-Omni
FastAPI or Node gateway
Next.js or Vite frontend
AudioWorklet
WebSocket
Docker Compose
Python benchmark scripts
```

---

## Build Order

Implement the project in this exact order.

---

## Phase 1 — Repository Setup

Create this structure:

```text
qwen3-omni-native-realtime/
  apps/
    web/
    gateway/
  configs/
  scripts/
  docs/
  docker-compose.yml
  README.md
  .env.example
```

Copy `ARCHITECTURE.md` into:

```text
docs/ARCHITECTURE.md
```

Create:

```text
docs/STREAMING_PROTOCOL.md
docs/LATENCY.md
docs/TASKS.md
```

Acceptance:

```text
Repository is clean.
No old pipeline code exists.
Docker Compose file exists.
README explains project goal.
```

---

## Phase 2 — Docker Compose

Create `docker-compose.yml`.

Services:

```text
qwen3-omni
gateway
web
```

The Qwen service must use vLLM-Omni and serve:

```text
Qwen/Qwen3-Omni-30B-A3B-Instruct
```

Example command shape:

```text
vllm serve Qwen/Qwen3-Omni-30B-A3B-Instruct --omni --host 0.0.0.0 --port 8091
```

Expose:

```text
8091:8091
8080:8080
3000:3000
```

Acceptance:

```text
docker compose up qwen3-omni
```

starts the Qwen server or fails with a clear dependency/model/GPU error.

No silent fallback is allowed.

---

## Phase 3 — Gateway Service

Build `apps/gateway`.

Recommended stack:

```text
Python
FastAPI
websockets
pydantic
uvicorn
```

Required routes:

```text
GET /health
GET /ready
WS /ws/realtime
```

Files:

```text
apps/gateway/src/main.py
apps/gateway/src/config.py
apps/gateway/src/health.py
apps/gateway/src/realtime/browser_ws.py
apps/gateway/src/realtime/qwen_client.py
apps/gateway/src/realtime/event_map.py
apps/gateway/src/realtime/session.py
apps/gateway/src/realtime/metrics.py
apps/gateway/src/realtime/audio.py
```

Gateway must:

```text
accept browser websocket
connect to Qwen realtime websocket
forward input audio chunks
commit audio
receive assistant text/audio events
normalize events
send normalized events to browser
collect metrics
handle errors
close sessions cleanly
```

Acceptance:

```text
Gateway starts with uvicorn.
GET /health returns alive.
GET /ready checks Qwen availability.
WS /ws/realtime accepts a connection.
```

---

## Phase 4 — Qwen Realtime Client

Implement `qwen_client.py`.

It must connect to:

```text
ws://qwen3-omni:8091/v1/realtime
```

Configurable env var:

```text
QWEN_REALTIME_URL
```

Implement methods:

```text
connect()
start_session()
append_audio(pcm16_base64)
commit_audio()
cancel_response()
close()
```

Map gateway events to Qwen realtime events.

The exact backend event names should be isolated in this file only.

Acceptance:

```text
Gateway can connect to Qwen.
Gateway can send audio chunks.
Gateway can commit audio.
Gateway can receive streamed response events.
```

---

## Phase 5 — Event Normalization

Implement `event_map.py`.

Frontend event names:

```text
session.start
audio.append
audio.commit
response.cancel
session.end
```

Browser output event names:

```text
session.ready
transcript.delta
assistant.text.delta
assistant.audio.delta
assistant.done
metrics.update
error
```

Acceptance:

```text
Frontend never needs to know raw Qwen event details.
Gateway logs raw events in debug mode only.
```

---

## Phase 6 — Audio Validation

Implement `audio.py`.

Validate incoming chunks:

```text
format = pcm16
channels = 1
sample_rate = 16000
base64 decodes successfully
chunk is not empty
chunk duration is within allowed bounds
```

Allowed chunk sizes:

```text
20 ms
40 ms
80 ms
120 ms
200 ms
```

Default:

```text
80 ms for browser
200 ms for smoke test
```

Acceptance:

```text
Invalid audio returns structured error.
Gateway does not forward malformed audio to Qwen.
```

---

## Phase 7 — WAV Smoke Test

Create:

```text
scripts/smoke_realtime_wav.py
```

Features:

```text
--direct
--gateway
--input path/to/input.wav
--chunk-ms 200
--output output.wav
--events events.json
--metrics metrics.json
```

Behavior:

```text
load 16 kHz mono PCM WAV
stream chunks
commit
receive assistant audio deltas
write output WAV
write events JSON
write metrics JSON
```

Acceptance:

```text
python scripts/smoke_realtime_wav.py --gateway --input test.wav
```

produces a real response audio file.

---

## Phase 8 — Browser Frontend

Build `apps/web`.

Recommended stack:

```text
Next.js
TypeScript
AudioWorklet
WebSocket
```

Required UI:

```text
Start Session
Stop Session
Push To Talk
Commit
Cancel Response
Speaker Selector
Transcript Panel
Assistant Text Panel
Metrics HUD
Raw Event Debug Panel
```

Frontend modules:

```text
src/audio/pcm.ts
src/audio/resampler.ts
src/audio/capture-worklet.ts
src/audio/playback-worklet.ts
src/realtime/client.ts
src/realtime/events.ts
src/components/CallControls.tsx
src/components/MetricsHud.tsx
src/components/TranscriptPanel.tsx
src/components/SpeakerSelector.tsx
```

Acceptance:

```text
Browser asks for microphone permission.
Browser captures audio.
Browser sends PCM16 chunks to gateway.
Browser receives assistant audio deltas.
Browser plays audio incrementally.
```

---

## Phase 9 — Playback Queue

Implement streaming playback with AudioWorklet.

Requirements:

```text
queue PCM chunks
start playback on first audio delta
track first_audio_played timestamp
clear queue on cancel
avoid waiting for full response
```

Acceptance:

```text
Assistant audio starts before response.done.
Cancel button stops current playback immediately.
```

---

## Phase 10 — Metrics

Implement metrics in both gateway and frontend.

Required timestamps:

```text
t_session_start
t_first_audio_chunk_sent
t_audio_commit_sent
t_first_transcript_delta
t_first_text_delta
t_first_audio_delta_received
t_first_audio_played
t_response_done
```

Required displayed metrics:

```text
commit_to_first_transcript_ms
commit_to_first_text_ms
commit_to_first_audio_delta_ms
commit_to_first_audio_played_ms
full_response_ms
```

Acceptance:

```text
Every session shows metrics in the browser.
Every smoke test writes metrics JSON.
```

---

## Phase 11 — Speaker Selection

Support speakers:

```text
Ethan
Chelsie
Aiden
```

The speaker must be selected before session start.

Acceptance:

```text
Changing speaker changes the session configuration.
If unsupported by backend, gateway returns clear error.
```

---

## Phase 12 — Text-Only Mode

Add mode:

```text
modalities = ["text"]
```

And speech mode:

```text
modalities = ["text", "audio"]
```

Acceptance:

```text
Text-only mode does not allocate playback queue.
Speech mode streams assistant audio.
Metrics compare both modes.
```

---

## Phase 13 — Barge-In Foundation

Implement local barge-in.

When user starts speaking while assistant audio is playing:

```text
clear playback queue
stop current audio
send response.cancel
mark t_barge_in
```

Acceptance:

```text
User interruption stops stale assistant audio immediately.
```

---

## Phase 14 — Benchmark Script

Create:

```text
scripts/benchmark_first_audio.py
```

Test matrix:

```text
chunk_ms: 40, 80, 120, 200
modalities: text, text+audio
speaker: Ethan, Chelsie, Aiden
runs: 5 minimum
```

Output:

```text
benchmark-results/first_audio.csv
benchmark-results/first_audio.md
benchmark-results/event_logs/
```

Acceptance:

```text
Benchmark report shows p50 and p95 first-audio latency.
Report clearly says whether 150 ms is realistic on this hardware.
```

---

## Phase 15 — Documentation

Create:

```text
docs/STREAMING_PROTOCOL.md
docs/LATENCY.md
docs/OPERATIONS.md
docs/TASKS.md
```

`STREAMING_PROTOCOL.md` must document all browser and gateway events.

`LATENCY.md` must document real measured latency.

`OPERATIONS.md` must explain Docker startup, GPU requirements, model download, health checks, and common failures.

`TASKS.md` must track every phase and completion status.

Acceptance:

```text
A new developer can clone the repo, read docs, run Docker, and understand the architecture.
```

---

## Final Build Acceptance

The implementation is complete only when this works:

```text
docker compose up
```

Then:

```text
Browser opens at http://localhost:3000
User clicks Start Session
User speaks
Browser streams PCM16 chunks to gateway
Gateway streams audio to Qwen3-Omni realtime API
Qwen3-Omni returns streamed text/audio events
Browser plays assistant audio before full completion
Metrics show first_audio_delta_ms and first_audio_played_ms
```

No external ASR.

No external TTS.

No fake streaming.

No old pipeline.

Pure Qwen3-Omni native realtime only.
 use Docker for simplicity


 Build simple React Vite project also from where we connect socket with simple ui
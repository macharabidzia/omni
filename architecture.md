# ARCHITECTURE.md — Pure Qwen3-Omni Native Realtime Voice Project

## 1. Project Goal

This repository implements a pure Qwen3-Omni native realtime voice system.

The system must allow a user to speak into a browser microphone and receive streamed spoken responses from Qwen3-Omni without using a separate ASR model, separate LLM, or separate TTS model.

This project is intentionally separate from any existing Vosk, CosyVoice, LiveKit, or custom 150 ms voice pipeline.

The goal is to answer one question clearly:

> What can Qwen3-Omni natively do when used correctly as a realtime omni-modal model?

---

## 2. Non-Negotiable Design Rules

### 2.1 Pure Qwen3-Omni

The runtime must use Qwen3-Omni directly for native realtime audio interaction.

Do not add:

* Vosk
* Whisper
* CosyVoice
* Piper
* Kokoro
* ElevenLabs
* external TTS
* external ASR
* old project voice pipeline modules
* fake streaming wrappers around full-file generation

The model backend is responsible for understanding user audio and generating assistant text/audio.

---

### 2.2 Realtime Means Streaming

The system must stream audio both ways.

User audio must be sent incrementally to the backend.

Assistant audio must be played incrementally as audio deltas arrive.

The system must not wait for the full assistant response before playback starts.

Correct behavior:

```text
User speaks
→ browser captures small PCM chunks
→ gateway forwards chunks to Qwen3-Omni realtime WebSocket
→ Qwen3-Omni emits text/audio events
→ browser starts playback from first audio delta
```

Incorrect behavior:

```text
User records full audio
→ server waits for full file
→ model generates full response
→ server generates full WAV
→ browser plays after everything is complete
```

---

## 3. Target Model

Primary model:

```text
Qwen/Qwen3-Omni-30B-A3B-Instruct
```

This model is used because it supports native omni-modal interaction and includes the components required for spoken response.

The project may later add optional modes for:

```text
Qwen/Qwen3-Omni-30B-A3B-Thinking
Qwen/Qwen3-Omni-30B-A3B-Captioner
```

But the first production path must use:

```text
Qwen/Qwen3-Omni-30B-A3B-Instruct
```

---

## 4. Runtime Stack

The project uses this stack:

```text
Browser frontend
  ↓
Realtime gateway
  ↓
vLLM-Omni Qwen3-Omni server
  ↓
Qwen3-Omni model
```

Recommended services:

```text
apps/web        Browser UI and audio capture/playback
apps/gateway    Realtime WebSocket gateway
qwen3-omni      vLLM-Omni model server
```

---

## 5. System Diagram

```text
┌──────────────────────────────────────────────────────────┐
│ Browser                                                  │
│                                                          │
│  Microphone                                              │
│      ↓                                                   │
│  AudioWorklet Capture                                    │
│      ↓                                                   │
│  PCM16 Mono Encoder                                      │
│      ↓                                                   │
│  Frontend WebSocket Client                               │
│      ↓                                                   │
└──────┼───────────────────────────────────────────────────┘
       │ client.audio.append / client.audio.commit
       ↓
┌──────────────────────────────────────────────────────────┐
│ Realtime Gateway                                         │
│                                                          │
│  Session Manager                                         │
│  Audio Format Validator                                  │
│  Chunk Pacer                                             │
│  Qwen Realtime Client                                    │
│  Event Normalizer                                        │
│  Metrics Collector                                       │
│                                                          │
└──────┼───────────────────────────────────────────────────┘
       │ input_audio_buffer.append / commit
       ↓
┌──────────────────────────────────────────────────────────┐
│ vLLM-Omni Server                                         │
│                                                          │
│  /v1/realtime WebSocket                                  │
│  Qwen/Qwen3-Omni-30B-A3B-Instruct                         │
│                                                          │
└──────┼───────────────────────────────────────────────────┘
       │ response.audio.delta / response.text.delta
       ↓
┌──────────────────────────────────────────────────────────┐
│ Browser                                                  │
│                                                          │
│  Assistant Text Display                                  │
│  AudioWorklet Playback Queue                             │
│  Latency HUD                                             │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 6. Repository Structure

```text
qwen3-omni-native-realtime/
  apps/
    web/
      src/
        audio/
          capture-worklet.ts
          playback-worklet.ts
          pcm.ts
          resampler.ts
        realtime/
          client.ts
          events.ts
        components/
          CallControls.tsx
          TranscriptPanel.tsx
          MetricsHud.tsx
          SpeakerSelector.tsx
        app/
          page.tsx
      package.json
      Dockerfile

    gateway/
      src/
        main.py
        config.py
        health.py
        realtime/
          browser_ws.py
          qwen_client.py
          event_map.py
          session.py
          metrics.py
          audio.py
      tests/
      pyproject.toml
      Dockerfile

  configs/
    qwen3_omni_deploy.yaml
    qwen3_omni_single_a100.yaml
    runtime.env.example

  scripts/
    smoke_realtime_wav.py
    benchmark_first_audio.py
    generate_test_wav.py

  docs/
    ARCHITECTURE.md
    STREAMING_PROTOCOL.md
    LATENCY.md
    OPERATIONS.md
    TASKS.md

  docker-compose.yml
  README.md
```

---

## 7. Docker Architecture

The project must run with Docker Compose.

Required services:

```text
qwen3-omni
gateway
web
```

The `qwen3-omni` service serves the model through vLLM-Omni.

The `gateway` service connects browser clients to the Qwen realtime WebSocket.

The `web` service provides the browser UI.

The gateway must not silently fallback to another model or fake backend.

If the Qwen server is unavailable, the gateway must return a clear error.

---

## 8. Audio Format Contract

### 8.1 Browser Input

The browser captures microphone audio as Float32 audio frames.

The frontend must convert this to:

```text
PCM16
mono
16 kHz target input
little-endian
```

The browser may capture at 44.1 kHz or 48 kHz, but the client must resample or the gateway must validate/resample.

Preferred design:

```text
browser captures native rate
→ browser converts/resamples to 16 kHz PCM16 mono
→ gateway validates format
→ gateway forwards to Qwen realtime API
```

---

### 8.2 Model Output

The Qwen realtime server may emit audio deltas at a different sample rate, commonly 24 kHz PCM.

The gateway must include output sample-rate metadata in normalized events.

The browser playback layer must queue and play the received PCM audio without waiting for the full response.

---

## 9. Realtime Event Contract

### 9.1 Browser → Gateway Events

The frontend sends JSON events.

```json
{
  "type": "session.start",
  "speaker": "Ethan",
  "modalities": ["text", "audio"],
  "input_sample_rate": 16000,
  "output_audio": true
}
```

```json
{
  "type": "audio.append",
  "audio_base64": "<base64 pcm16 chunk>",
  "sample_rate": 16000,
  "channels": 1,
  "format": "pcm16"
}
```

```json
{
  "type": "audio.commit"
}
```

```json
{
  "type": "response.cancel"
}
```

```json
{
  "type": "session.end"
}
```

---

### 9.2 Gateway → Browser Events

The gateway emits normalized events.

```json
{
  "type": "session.ready",
  "session_id": "..."
}
```

```json
{
  "type": "transcript.delta",
  "text": "..."
}
```

```json
{
  "type": "assistant.text.delta",
  "text": "..."
}
```

```json
{
  "type": "assistant.audio.delta",
  "audio_base64": "<base64 pcm chunk>",
  "sample_rate": 24000,
  "channels": 1,
  "format": "pcm16"
}
```

```json
{
  "type": "assistant.done"
}
```

```json
{
  "type": "metrics.update",
  "metrics": {
    "first_transcript_ms": 0,
    "first_text_ms": 0,
    "first_audio_delta_ms": 0,
    "first_audio_played_ms": 0
  }
}
```

```json
{
  "type": "error",
  "code": "QWEN_UNAVAILABLE",
  "message": "Qwen3-Omni realtime server is not available."
}
```

---

## 10. Gateway Responsibilities

The gateway is not optional.

It must handle:

* browser WebSocket sessions
* Qwen realtime WebSocket connection
* event translation
* audio format validation
* optional audio chunk coalescing
* response cancellation
* structured errors
* metrics
* health checks
* backpressure
* logging

The browser must not be tightly coupled to raw Qwen event names.

The gateway normalizes the API so the frontend can remain stable even if the backend event names change.

---

## 11. Frontend Responsibilities

The frontend must provide:

* microphone permission handling
* start/stop session
* push-to-talk mode
* optional auto-commit mode
* PCM16 conversion
* realtime WebSocket client
* assistant audio playback queue
* transcript panel
* assistant text panel
* latency HUD
* speaker selector
* debug event log export

The frontend must not fake streaming by waiting for full audio.

---

## 12. Latency Metrics

Every session must collect:

```text
t_session_start
t_microphone_started
t_first_audio_chunk_sent
t_audio_commit_sent
t_first_transcript_delta
t_first_text_delta
t_first_audio_delta_received
t_first_audio_played
t_response_done
```

Derived metrics:

```text
mic_to_first_transcript_ms
commit_to_first_text_ms
commit_to_first_audio_delta_ms
commit_to_first_audio_played_ms
full_response_ms
```

The main benchmark metric is:

```text
audio.commit → first assistant audio played
```

Do not claim 150 ms until measured.

The first goal is real native streaming.

The second goal is stable low latency.

The third goal is optimization.

---

## 13. Barge-In Foundation

The first version must support local playback interruption.

When user speech starts during assistant playback:

```text
clear browser playback queue
send response.cancel to gateway
gateway forwards cancellation if supported
mark t_barge_in
```

If model-side cancellation is not supported or unreliable, the system must still stop local playback immediately and prevent stale audio from continuing.

---

## 14. Health Checks

The gateway must expose:

```text
GET /health
GET /ready
```

Health states:

```text
container_alive
qwen_unreachable
qwen_loading
qwen_ready
qwen_failed
```

The frontend must not start a realtime session unless the gateway reports ready.

---

## 15. Error Handling

The system must handle:

* Qwen server unavailable
* model still loading
* model OOM
* websocket disconnect
* malformed event
* unsupported audio format
* microphone permission denied
* audio playback failure
* timeout waiting for first audio
* browser sends audio too fast
* gateway output queue overflow

Errors must be visible in the UI and logged in the gateway.

Silent fallback is forbidden.

---

## 16. Benchmarking

Required benchmark scripts:

```text
scripts/smoke_realtime_wav.py
scripts/benchmark_first_audio.py
```

The WAV smoke test must:

* load a 16 kHz mono PCM WAV
* connect to gateway
* stream chunks
* commit audio
* receive assistant audio deltas
* save output WAV
* save event log JSON
* print latency summary

The first-audio benchmark must test:

```text
chunk_ms = 40, 80, 120, 200
speaker = Ethan, Chelsie, Aiden
modalities = text only, text+audio
concurrency = 1 first
```

Required report:

```text
benchmark-results/
  first_audio.csv
  first_audio.md
  event_logs/
```

---

## 17. Development Milestones

### Milestone 1 — Model Server Boots

Qwen3-Omni runs in Docker through vLLM-Omni.

Acceptance:

```text
docker compose up qwen3-omni
```

starts the model server and exposes realtime API.

---

### Milestone 2 — Direct WAV Smoke Test

A script streams a WAV file to the model realtime endpoint and saves response audio.

Acceptance:

```text
python scripts/smoke_realtime_wav.py --direct
```

produces:

```text
output.wav
events.json
metrics.json
```

---

### Milestone 3 — Gateway WAV Smoke Test

The same WAV file is streamed through the gateway.

Acceptance:

```text
python scripts/smoke_realtime_wav.py --gateway
```

produces streamed assistant audio and metrics.

---

### Milestone 4 — Browser Microphone Input

The browser captures microphone audio and sends PCM chunks to the gateway.

Acceptance:

```text
User speaks
→ gateway receives audio chunks
→ gateway forwards to Qwen
```

---

### Milestone 5 — Browser Streaming Playback

The browser plays assistant audio deltas as they arrive.

Acceptance:

```text
User speaks
→ Qwen responds
→ browser plays response before full completion
```

---

### Milestone 6 — Metrics HUD

The UI displays live timing metrics.

Acceptance:

```text
first_transcript_ms
first_text_ms
first_audio_delta_ms
first_audio_played_ms
```

are visible per session.

---

### Milestone 7 — Speaker Selection

The UI allows speaker selection.

Acceptance:

```text
Ethan
Chelsie
Aiden
```

can be selected before session start.

---

### Milestone 8 — Barge-In Foundation

The browser can stop local assistant playback when user starts speaking again.

Acceptance:

```text
User interrupts assistant
→ playback queue clears
→ stale audio stops
```

---

## 18. What This Project Is Not

This project is not:

* a Vosk ASR project
* a CosyVoice TTS project
* a LiveKit media server project
* a SIP phone project
* a fake demo that records full audio then plays full output
* a benchmark that claims 150 ms before measurement
* a wrapper around the old pipeline

This project is a pure Qwen3-Omni native realtime lab.

---

## 19. Final Acceptance Definition

The project is successful when:

```text
1. Qwen3-Omni runs through Docker.
2. Browser microphone audio streams to the gateway.
3. Gateway streams audio to Qwen3-Omni realtime API.
4. Qwen3-Omni returns streamed assistant audio.
5. Browser plays assistant audio before full response completion.
6. Metrics show first audio delta and first audio played latency.
7. No external ASR or TTS is used.
8. No old pipeline modules are imported.
```

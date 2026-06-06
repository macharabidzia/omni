# steps.md — LiveKit-Only Qwen3-Omni Implementation Steps

## Loop Rule

Development follows this loop:

```text
Review current code and runtime behavior
-> update the highest-value tasks
-> implement only the active transport/runtime changes
-> run focused tests plus a live probe when the change touches the browser path
-> record blockers truthfully
```

No fake backend or transport shim counts as production success.

The browser transport is LiveKit only.
There is no browser websocket audio gateway in this phase.

---

## Step 1 — Runtime Contract

Goal:

Define one supported realtime browser path.

Contract:

```text
Browser mic
-> LiveKit
-> LiveKit worker
-> RealtimeSession
-> Qwen realtime backend
-> LiveKit assistant track
-> Browser playback
```

Acceptance:

```text
no browser websocket route exists
no direct browser-to-model path exists
control plane is HTTP only
worker is the only browser-media bridge
```

---

## Step 2 — Config

Goal:

Validate the runtime for LiveKit plus Qwen.

Required config:

```yaml
LIVEKIT_URL:
LIVEKIT_API_KEY:
LIVEKIT_API_SECRET:
LIVEKIT_ROOM:
LIVEKIT_AGENT_ID:
QWEN_REALTIME_URL:
QWEN_HEALTH_URL:
QWEN_MODEL:
INPUT_AUDIO_SAMPLE_RATE:
OUTPUT_AUDIO_SAMPLE_RATE:
LIVEKIT_OUTPUT_SAMPLE_RATE:
LIVEKIT_OUTPUT_FRAME_MS:
LIVEKIT_OUTPUT_QUEUE_MS:
TARGET_FIRST_AUDIO_P95_MS: 500
```

Acceptance:

```text
missing LiveKit config fails clearly
missing Qwen config fails clearly
worker logs the LiveKit target it is using
readiness does not report ready without Qwen and the LiveKit worker
```

---

## Step 3 — Control Plane

Goal:

Keep the FastAPI service HTTP-only.

Required endpoints:

```yaml
GET /health
GET /ready
POST /livekit/session
```

Acceptance:

```text
no websocket endpoint remains in the gateway
/livekit/session returns browser token + room metadata
/ready checks Qwen and LiveKit worker state
```

---

## Step 4 — LiveKit Worker

Goal:

Make the worker the only browser-facing realtime bridge.

Responsibilities:

```text
join the configured LiveKit room
publish the assistant audio track
receive browser control messages over data packets
stream microphone audio frames into RealtimeSession
publish assistant audio back through LiveKit
forward assistant text/metrics/errors over LiveKit data packets
```

Acceptance:

```text
worker connects with env-configured LiveKit credentials
worker can subscribe to browser mic audio
worker can publish assistant audio
worker shutdown clears room/session state cleanly
```

---

## Step 5 — Session Runtime

Goal:

Keep `RealtimeSession` transport-agnostic, driven by the worker event sink.

Acceptance:

```text
RealtimeSession has no browser websocket shim
assistant output buffering before commit still works
cancel/interrupt still restarts stale Qwen realtime sessions
text-only fallback remains explicit and test-covered
```

---

## Step 6 — Browser Client

Goal:

Use LiveKit directly from the web app.

Acceptance:

```text
browser fetches a LiveKit token from /livekit/session
browser publishes microphone audio to LiveKit
browser subscribes to the assistant audio track
browser receives assistant text/metrics/errors from LiveKit data packets
no browser code attempts to open a gateway websocket
```

---

## Step 7 — Probes And Benchmarks

Goal:

Keep verification aligned to the supported transport.

Required probes:

```text
direct Qwen smoke test for backend behavior
LiveKit connectivity smoke for worker/browser credentials
browser LiveKit probe for real playback
```

Acceptance:

```text
dead gateway websocket CLI modes are removed
docs point browser verification to LiveKit probes
latency reporting uses commit-to-playback for audio sessions
```

---

## Step 8 — Production Gate

Production-ready means:

```text
Qwen health probe passes
Qwen realtime session probe passes
LiveKit worker joins the configured room
assistant track is published
browser LiveKit session can start and play assistant audio
first-audio metrics are reported
no browser websocket transport code remains
```

Anything less is either `warming`, `degraded`, or `failed`, not done.

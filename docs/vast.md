# Vast.ai Runtime

This project now supports one deployment path on Vast.ai:

```text
supervisor
-> qwen model server on 127.0.0.1:17091
-> gateway on 127.0.0.1:17080
-> web app on 127.0.0.1:17070
-> Caddy HTTPS on 3000/8080/8091
-> external LiveKit server on Snel VPS
```

## Required Vast ports

Open these container ports when you create the instance:

- `3000`
- `8080`
- `8091`

SSH remains on `22`.

## Bootstrap

```bash
cd /workspace/omni
cp .env.example /workspace/.env  # first time only, if needed
scripts/bootstrap_vast.sh
scripts/install_vast_supervisor.sh
scripts/status_vast.sh
```

## Current instance URLs

- web: `https://82.66.51.122:12289/?token=$OPEN_BUTTON_TOKEN`
- gateway ready: `https://82.66.51.122:56242/ready?token=$OPEN_BUTTON_TOKEN`
- qwen health: `https://82.66.51.122:19571/health?token=$OPEN_BUTTON_TOKEN`

The browser client must be opened over HTTPS. Raw `http://$PUBLIC_IPADDR:$VAST_TCP_PORT_3000` is not a valid microphone origin in Chrome-based browsers.

LiveKit setup details live in [model.md](model.md). The Vast instance only runs the worker-side runtime and connects to the external LiveKit server described there.

To verify the public browser path end to end with a real speech file:

```bash
scripts/smoke_public_livekit_e2e.sh /path/to/real-speech.wav
```

## No-model local smoke

When a hosted/local real Qwen model is unavailable, you can still verify the direct realtime protocol path with the built-in stub backend:

```bash
python scripts/smoke_stub_qwen_realtime.py --output-dir tmp/stub-qwen-smoke
```

That command:

- starts `scripts/stub_qwen_server.py`
- generates a short 16 kHz test WAV if needed
- runs `scripts/smoke_realtime_wav.py` against the stub `/v1/realtime` endpoint
- writes `events.json`, `metrics.json`, and `assistant-output.wav` under the chosen output directory

If you want to exercise the full gateway/worker/browser path without a real model, run `scripts/stub_qwen_server.py` and point:

- `QWEN_REALTIME_URL` to `ws://127.0.0.1:17091/v1/realtime`
- `QWEN_CHAT_URL` to `http://127.0.0.1:17091/v1/chat/completions`
- `QWEN_HEALTH_URL` to `http://127.0.0.1:17091/health`

Then use the existing browser Playwright probes in `apps/web/scripts/` or the normal dev startup scripts.

## Capacity planning

Until real-model measurements are collected, keep the runtime in the safest posture:

- one active browser session per LiveKit worker
- one warm Qwen realtime session per active browser session
- scale out with more worker/model instances instead of multiplexing more sessions into one worker

Operator worksheet for each GPU/model build:

```text
GPU type:
model id:
warm VRAM used:
cold-start time:
steady-state p95 commit_to_qwen_first_audio_ms:
proven max concurrent realtime sessions:
voice response token cap:
```

Current local default for the deployed Qwen runtime:

- `scripts/run_qwen_model.sh` uses `configs/qwen3_omni_single_a100_async_fastaudio.yaml` by default
- that config keeps stage-0 voice generation at `default_sampling_params.max_tokens: 16`
- fallback text/chat mode remains separately capped by `QWEN_TEXT_MAX_COMPLETION_TOKENS`

If capacity is exceeded:

- do not raise worker concurrency first
- keep rejecting a second browser participant with `ROOM_BUSY`
- let readiness go `degraded` or `failed` when Qwen is not healthy
- drain old workers before replacement instead of overcommitting a live room

## Timeout and overload behavior

Relevant runtime guards now include:

- websocket open/send timeout: `QWEN_REQUEST_TIMEOUT_SECONDS`
- first assistant audio timeout: `QWEN_FIRST_AUDIO_TIMEOUT_SECONDS`
- total assistant response timeout: `QWEN_TOTAL_RESPONSE_TIMEOUT_SECONDS`
- browser idle timeout: `LIVEKIT_BROWSER_IDLE_TIMEOUT_SECONDS`
- LiveKit drain timeout: `LIVEKIT_DRAIN_TIMEOUT_SECONDS`
- max turn length: `LIVEKIT_MAX_TURN_MS`
- LiveKit assistant queue hard cap: `LIVEKIT_OUTPUT_QUEUE_MS`

The gateway startup script now enables a Qwen startup prewarm pass by default:

- `QWEN_PREWARM_ON_STARTUP=true`
- `QWEN_PREWARM_MAX_WAIT_SECONDS=60`
- `QWEN_PREWARM_RETRY_INTERVAL_SECONDS=2`

That prewarm path reuses the existing deep Qwen probe so the deployment can pay the initial inference warmup cost before the first real browser turn when the model is reachable.

When the assistant queue reaches its hard cap, the publisher drops overloaded pending frames instead of letting delayed speech accumulate into seconds of lag. That is a latency-preserving failure mode, not a quality optimization, so investigate queue-cap warnings before increasing load.

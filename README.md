# Qwen3-Omni Native Realtime

Pure Qwen3-Omni native realtime voice stack:

- `qwen3-omni`: vLLM-Omni model server for `Qwen/Qwen3-Omni-30B-A3B-Instruct`
- `apps/gateway`: FastAPI control plane for health, readiness, and LiveKit browser tokens
- `apps/gateway/src/livekit/worker.py`: LiveKit worker that bridges room audio/data to Qwen realtime
- `apps/web`: React + Vite browser client that joins LiveKit directly and uses the control plane only for HTTP
- `scripts/`: direct Qwen smoke tests, public LiveKit browser probes, and model patch tooling

This repo is configured for one runtime path on Vast.ai:

```text
Vast.ai process runtime
-> supervisor
-> local Qwen model on 127.0.0.1:17091
-> local gateway on 127.0.0.1:17080
-> local Vite web app on 127.0.0.1:17070
-> Caddy HTTPS on external ports 3000/8080/8091
-> external LiveKit server on Snel VPS
```

## Requirements

- Vast.ai instance with a CUDA-capable GPU
- Open ports for container `3000`, `8080`, and `8091`
- Enough VRAM for `Qwen/Qwen3-Omni-30B-A3B-Instruct` (see [model.md](model.md))
- `uv`
- Node.js 20+

This repo intentionally does not keep a Docker Compose deployment path. Vast already runs inside a container and this project now uses native processes plus supervisor only.

## Vast Quick Start

1. Review `/workspace/.env` or replace it from `.env.example`.
2. Optional but recommended: set `HF_TOKEN` in `/workspace/.env` for faster Hugging Face downloads.
3. Bootstrap the instance:

```bash
scripts/bootstrap_vast.sh
```

This creates `.venv` and `.venv-qwen`, installs the web dependencies, installs the vLLM-Omni runtime, and prefetches `Qwen3-Omni-30B-A3B-Instruct` into `models/` if it is missing.

4. Install the supervisor services:

```bash
scripts/install_vast_supervisor.sh
```

5. Check service state and URLs:

```bash
scripts/status_vast.sh
```

For this current instance, the public URLs are:

- Web app: `https://82.66.51.122:12289/?token=$OPEN_BUTTON_TOKEN`
- Gateway ready check: `https://82.66.51.122:56242/ready?token=$OPEN_BUTTON_TOKEN`
- Qwen health: `https://82.66.51.122:19571/health?token=$OPEN_BUTTON_TOKEN`

The web app must be opened over HTTPS so the browser exposes microphone capture. The direct raw HTTP port is not enough for `getUserMedia()`.

## Local Development

The direct dev commands are still available if you want to run pieces by hand:

```bash
scripts/run_qwen_model.sh
scripts/run_gateway_dev.sh
scripts/run_worker_dev.sh
scripts/run_web_dev.sh
```

Keep the Qwen model process on `127.0.0.1:17091` running across ordinary gateway and web iteration. Normal gateway/web code changes should not require a model restart unless model-serving behavior or model boot configuration changed.

Gateway tests:

```bash
(cd apps/gateway && ../../.venv/bin/pytest)
```

Frontend build:

```bash
(cd apps/web && npm run build)
```

Generate a 16 kHz WAV:

```bash
./.venv/bin/python scripts/generate_test_wav.py --output /tmp/test.wav
```

Run the LiveKit connectivity probe:

```bash
./.venv/bin/python scripts/smoke_livekit_connect.py --worker
```

Run the browser LiveKit probe against the public Vast HTTPS web app:

```bash
cd apps/web
node scripts/playwright-livekit-check.mjs \
  https://82.66.51.122:12289/?token=$OPEN_BUTTON_TOKEN \
  /tmp/test.wav \
  /tmp/livekit-check.json
```

Run the full public WAV-to-mic-to-LiveKit end-to-end check:

```bash
scripts/smoke_public_livekit_e2e.sh /path/to/real-speech.wav
```

That command normalizes the input to a 16 kHz mono PCM16 WAV, runs the browser session over the public HTTPS Vast URL, captures the returned assistant audio, and analyzes the capture for clipping or internal silence gaps.

Run the direct realtime smoke test:

```bash
./.venv/bin/python scripts/smoke_realtime_wav.py \
  --input /tmp/test.wav \
  --qwen-url ws://127.0.0.1:17091/v1/realtime
```

The default model launch path uses `configs/qwen3_omni_single_a100_async_fastaudio.yaml`, applies a local `vllm-omni` route patch so `/v1/realtime` can run with `async_chunk=true`, and applies a local `qwen3_omni` stage patch so code2wav can use `initial_codec_chunk_frames=2` with a steady `codec_chunk_frames=2` cadence.

The production browser path is now LiveKit-only: the browser publishes microphone audio to LiveKit, the worker streams that audio into Qwen continuously, assistant text/metrics flow back over LiveKit data packets, and assistant audio is published back to the browser as a LiveKit audio track. The worker still buffers assistant output until speech commit so the low-latency turn path is preserved.

## Docs

- [docs/architecture.md](docs/architecture.md)
- [docs/vast.md](docs/vast.md)
- [docs/model.md](docs/model.md)
- [docs/steps.md](docs/steps.md)
- [docs/AGENTS.md](docs/AGENTS.md)
- [tasks.md](tasks.md)

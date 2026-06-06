# Qwen3-Omni Native Realtime

Pure Qwen3-Omni native realtime voice stack:

- `qwen3-omni`: vLLM-Omni model server for `Qwen/Qwen3-Omni-30B-A3B-Instruct`
- `apps/gateway`: FastAPI control plane for health, readiness, and LiveKit browser tokens
- `apps/gateway/src/livekit/worker.py`: LiveKit worker that bridges room audio/data to Qwen realtime
- `apps/web`: React + Vite browser client that joins LiveKit directly and uses the control plane only for HTTP
- `scripts/`: direct Qwen smoke tests, LiveKit connect probes, and model patch tooling

## Goal

This project answers one question: what Qwen3-Omni can do natively when used as a realtime audio model without separate ASR or TTS components.

## Requirements

- Docker with `docker compose`
- NVIDIA Container Toolkit and a CUDA-capable GPU
- Enough VRAM for `Qwen/Qwen3-Omni-30B-A3B-Instruct` (see [model.md](model.md))

For local non-Docker checks in this repo:

- Python 3.11+
- Node.js 20+

## Quick Start

1. Copy `.env.example` to `.env`.
2. Optional but recommended: set `HF_TOKEN` in `.env` for better Hugging Face rate limits.
3. If the first Qwen boot spends too long downloading weights, prefetch them into the repo-mounted `models/` directory first:

```bash
python3 -m pip install -U "huggingface_hub[cli]"
scripts/prefetch_qwen_model.sh
```

4. Then set `QWEN_MODEL=/workspace/models/Qwen3-Omni-30B-A3B-Instruct` in `.env`.
5. Keep the default single-A100 deploy profile:

```bash
QWEN_DEPLOY_CONFIG=/workspace/configs/qwen3_omni_single_a100.yaml
```

For a larger multi-GPU host, override it with:

```bash
QWEN_DEPLOY_CONFIG=/workspace/configs/qwen3_omni_deploy.yaml
```

6. Start the stack:

```bash
docker compose up --build
```

For latency instrumentation during benchmarking, set:

```bash
QWEN_VLLM_FLAGS=--log-stats
```

7. Open `http://localhost:3000`.

Services:

- Qwen model server: `http://localhost:8091`
- Control plane: `http://localhost:8080`
- LiveKit worker: `worker` service in `docker compose`
- Web app: `http://localhost:3000`

## Local Development

Gateway:

```bash
python3 -m pip install -e 'apps/gateway[dev]'
uvicorn src.main:app --app-dir apps/gateway --host 0.0.0.0 --port 8080
```

Worker:

```bash
python3 -m pip install -e 'apps/gateway[dev]'
cd apps/gateway
python3 -m src.livekit.worker
```

Web:

```bash
cd apps/web
npm ci
npm run dev -- --host 0.0.0.0 --port 3000
```

Persistent model workflow:

```bash
scripts/run_qwen_model.sh
scripts/run_gateway_dev.sh
scripts/run_worker_dev.sh
scripts/run_web_dev.sh
```

Keep the Qwen model process on `:8091` running across ordinary gateway and web iteration. Normal gateway/web code changes should not require a model restart unless model-serving behavior or model boot configuration changed.

## Verification

Gateway tests:

```bash
cd apps/gateway
pytest
```

Frontend production build:

```bash
cd apps/web
npm run build
```

Generate a 16 kHz WAV:

```bash
python3 scripts/generate_test_wav.py --output /tmp/test.wav
```

Run the LiveKit connect probe:

```bash
LIVEKIT_URL=ws://185.62.58.164:7880 \
LIVEKIT_API_KEY=devkey \
LIVEKIT_API_SECRET=devsecret \
LIVEKIT_ROOM=omni-room \
python3 scripts/smoke_livekit_connect.py --worker
```

The default model launch path uses `configs/qwen3_omni_single_a100_async_fastaudio.yaml`, applies a local `vllm-omni` route patch so `/v1/realtime` can run with `async_chunk=true`, and applies a local `qwen3_omni` stage patch so code2wav can use `initial_codec_chunk_frames=2` with a steady `codec_chunk_frames=2` cadence.

The production browser path is now LiveKit-only: the browser publishes microphone audio to LiveKit, the worker streams that audio into Qwen continuously, assistant text/metrics flow back over LiveKit data packets, and assistant audio is published back to the browser as a LiveKit audio track. The worker still buffers assistant output until speech commit so the low-latency turn path is preserved.

Run the browser LiveKit probe:

```bash
cd apps/web
node scripts/playwright-livekit-check.mjs \
  http://127.0.0.1:5173 \
  /tmp/test.wav \
  /tmp/livekit-check.json
```

Run the direct model smoke test against the native realtime endpoint:

```bash
QWEN_MODEL=/workspace/models/Qwen3-Omni-30B-A3B-Instruct \
python3 scripts/smoke_realtime_wav.py \
  --input /tmp/test.wav \
  --qwen-url ws://localhost:8091/v1/realtime
```

## RunPod

RunPod overrides and SSH examples are staged in:

- [configs/runtime.env.example](configs/runtime.env.example)
- [configs/runpod_ssh_config.example](configs/runpod_ssh_config.example)

## Docs

- [docs/architecture.md](docs/architecture.md)
- [docs/steps.md](docs/steps.md)
- [docs/AGENTS.md](docs/AGENTS.md)
- [tasks.md](tasks.md)

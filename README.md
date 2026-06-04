# Qwen3-Omni Native Realtime

Pure Qwen3-Omni native realtime voice stack:

- `qwen3-omni`: vLLM-Omni model server for `Qwen/Qwen3-Omni-30B-A3B-Instruct`
- `apps/gateway`: FastAPI WebSocket gateway that normalizes browser and model events
- `apps/web`: React + Vite browser client with microphone capture, streaming playback, metrics, and debug log export
- `scripts/`: direct and gateway smoke tests plus first-audio benchmark tooling

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
- Gateway: `http://localhost:8080`
- Web app: `http://localhost:3000`

## Local Development

Gateway:

```bash
python3 -m pip install -e 'apps/gateway[dev]'
uvicorn src.main:app --app-dir apps/gateway --host 0.0.0.0 --port 8080
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

Run the gateway smoke test once Qwen is live:

```bash
python3 scripts/smoke_realtime_wav.py --gateway --input /tmp/test.wav
```

The smoke client defaults to `20 ms` upload chunks.

The default model launch path uses `configs/qwen3_omni_single_a100_async_fastaudio.yaml`, applies a local `vllm-omni` route patch so `/v1/realtime` can run with `async_chunk=true`, and applies a local `qwen3_omni` stage patch so code2wav can use `initial_codec_chunk_frames=2` with a steady `codec_chunk_frames=2` cadence.

The gateway keeps browser turn semantics by buffering assistant text/audio until `audio.commit`, and the live web client auto-commits after local silence detection instead of using push-to-talk. On the target A100, the warmed real proxy path now measures `p50 465.46 ms / p95 487.79 ms / p99 490.76 ms` first playable audio after commit at `20 ms` chunks with `sample_count 5`; evidence is in `benchmark-results/live_gateway_proxy_chunk2x2_warm/`. A real page check against `https://7lycxebyx5swhu-5173.proxy.runpod.net/` recorded `Commit to Audio Played 494.1 ms`, and its debug log shows no early playback drain between the first and second assistant audio chunks; evidence is in `tmp/playwright-live-check/result-after-chunk2x2.json`.

Run the direct model smoke test against the native realtime endpoint:

```bash
QWEN_MODEL=/workspace/models/Qwen3-Omni-30B-A3B-Instruct \
python3 scripts/smoke_realtime_wav.py \
  --direct \
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

# Operations

## Local Docker Startup

1. Copy `.env.example` to `.env`.
2. For a single 80 GB A100 host, keep:

```bash
QWEN_DEPLOY_CONFIG=/workspace/configs/qwen3_omni_single_a100.yaml
```

For a multi-GPU host, override it with:

```bash
QWEN_DEPLOY_CONFIG=/workspace/configs/qwen3_omni_deploy.yaml
```

3. Start the stack:

```bash
docker compose up --build
```

4. Verify health:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/ready
```

## GPU Requirements

`Qwen/Qwen3-Omni-30B-A3B-Instruct` is heavy. The model notes in `model.md` indicate roughly 78.85 GB BF16 minimum for the instruct model. The provided RunPod target is an A100 SXM.

The checked-in `configs/qwen3_omni_single_a100.yaml` profile was locally verified on June 3, 2026 against a single `NVIDIA A100-SXM4-80GB` with the official `vllm 0.20.2` and `vllm-omni 0.20.0` runtime stack. That profile is intentionally conservative: it trades throughput for enough headroom to let spoken realtime requests finish without crashing the thinker-to-talker handoff.

## Model Download and Build

`configs/qwen3-omni.Dockerfile` now uses the official realtime stack:

- `torch 2.11.0` from the PyTorch `cu129` index
- the official `vllm 0.20.2+cu129` release wheel
- the published `vllm-omni 0.20.0` wheel

For realtime browser sessions, the launch command also forces `--no-async-chunk`. Current upstream `vllm-omni` docs note that `/v1/realtime` is unsupported while `async_chunk` is enabled for Qwen3-Omni.

The first model boot is download-bound. On June 3, 2026, the Hugging Face metadata for `Qwen/Qwen3-Omni-30B-A3B-Instruct` reported about `65.7 GiB` of model artifacts. Expect the Qwen container to stay off-port until those weights are present locally.

For deterministic first boot, prefetch the model into the repo-mounted `models/` directory and point `QWEN_MODEL` at that local path:

```bash
python3 -m pip install -U "huggingface_hub[cli]"
scripts/prefetch_qwen_model.sh
```

Then set:

```bash
QWEN_MODEL=/workspace/models/Qwen3-Omni-30B-A3B-Instruct
```

If you are using the single-A100 path, also keep:

```bash
QWEN_DEPLOY_CONFIG=/workspace/configs/qwen3_omni_single_a100.yaml
```

The Compose file mounts `./models` into the Qwen container as `/workspace/models`, so the server can start directly from the prefetched checkout.
The default runtime environment also sets `HF_HUB_DISABLE_XET=1`. During local verification, that path behaved more predictably than the Xet-backed downloader for large shard fetches on this host. If you need authenticated Hub access for rate limits, set `HF_TOKEN` in `.env`.

This change is intentional. During local verification on June 3, 2026:

- a naive `pip install vllm==0.20.x` path resolved to a newer CUDA 13-flavored PyTorch runtime and failed on this host
- the official `cu129` wheel path imported cleanly, exposed `vllm serve ... --omni --no-async-chunk`, and started real `Qwen/Qwen3-Omni-30B-A3B-Instruct` initialization on an A100 host with NVIDIA driver `550.127.05`
- the older Qwen-maintained `qwen3_omni` fork was rejected for this repo because it did not expose the `/v1/realtime` surface used here, and its hardcoded precompiled wheel URL returned `404`
- an unauthenticated `HF_XET_HIGH_PERFORMANCE=1` retry did not resume a partial local download cleanly during verification, so it is not enabled by default here
- a direct `hf download` with `HF_HUB_DISABLE_XET=1` successfully pulled a 5 GB model shard to local disk in about 15 seconds on the same host, so the prefetch helper uses that path

If the build fails, the failure should be explicit. There is no fallback model path in Compose or the gateway.

## Health States

- `container_alive`
- `qwen_unreachable`
- `qwen_loading`
- `qwen_ready`
- `qwen_failed`

`GET /health` reports container liveness.

`GET /ready` checks Qwen availability through `QWEN_HEALTH_URL` and `QWEN_REALTIME_URL`.

## Common Failures

- missing NVIDIA runtime or GPU access
- installing an unpinned `vllm` stack instead of the repo's validated `cu129` wheel path
- OOM during model load
- inability to download model weights
- websocket handshake failure on `/v1/realtime`
- browser microphone permission denied
- unsupported PCM chunk format from client

## RunPod SSH

Template SSH config is in [configs/runpod_ssh_config.example](../configs/runpod_ssh_config.example).

Direct TCP SSH:

```bash
ssh root@195.26.233.65 -p 57403 -i ~/.ssh/id_ed25519
```

Proxy SSH:

```bash
ssh y977m2sllhl4w8-644114c5@ssh.runpod.io -i ~/.ssh/id_ed25519
```

## RunPod Port Overrides

The current RunPod notes in `model.md` expose:

- proxied web port `5173`
- proxied gateway port `8000`

For that environment, set:

```bash
WEB_PORT=5173
GATEWAY_PORT=8000
VITE_GATEWAY_HTTP_URL=https://<your-runpod-gateway-host>
VITE_GATEWAY_WS_URL=wss://<your-runpod-gateway-host>/ws/realtime
```

`configs/runtime.env.example` includes the same RunPod-oriented variables so you can stage the overrides without editing the main `.env.example`.

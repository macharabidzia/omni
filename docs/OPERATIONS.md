# Operations

## Local Docker Startup

1. Copy `.env.example` to `.env`.
2. Start the stack:

```bash
docker compose up --build
```

3. Verify health:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/ready
```

## GPU Requirements

`Qwen/Qwen3-Omni-30B-A3B-Instruct` is heavy. The model notes in `model.md` indicate roughly 78.85 GB BF16 minimum for the instruct model. The provided RunPod target is an A100 SXM.

## Model Download and Build

`configs/qwen3-omni.Dockerfile` follows the Qwen guidance from `model.md`:

- clone the `qwen3_omni` vLLM branch
- install the vLLM CUDA dependencies
- install `transformers` from source
- install `qwen-omni-utils`

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
- OOM during model load
- inability to download model weights
- websocket handshake failure on `/v1/realtime`
- browser microphone permission denied
- unsupported PCM chunk format from client

## RunPod SSH

Template SSH config is in [configs/runpod_ssh_config.example](/mnt/d/omni/configs/runpod_ssh_config.example).

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


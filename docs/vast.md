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

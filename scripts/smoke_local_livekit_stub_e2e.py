#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY_ROOT = REPO_ROOT / "apps" / "gateway"
WEB_ROOT = REPO_ROOT / "apps" / "web"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a local browser -> LiveKit -> worker -> gateway -> stub Qwen -> LiveKit -> browser "
            "smoke check without a real model backend."
        )
    )
    parser.add_argument("--livekit-url", default="ws://185.62.58.164:7880")
    parser.add_argument("--livekit-api-key", default="devkey")
    parser.add_argument("--livekit-api-secret", default="devsecret")
    parser.add_argument("--livekit-room", default="omni-room")
    parser.add_argument("--livekit-agent-id", default="omni-worker")
    parser.add_argument("--gateway-host", default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=18080)
    parser.add_argument("--web-host", default="127.0.0.1")
    parser.add_argument("--web-port", type=int, default=13000)
    parser.add_argument("--stub-host", default="127.0.0.1")
    parser.add_argument("--stub-port", type=int, default=17191)
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=Path("tmp/local-livekit-stub-e2e"))
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    return parser.parse_args()


def wait_for_http_json(
    *,
    url: str,
    timeout_seconds: float,
    predicate,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    last_payload: dict | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            last_payload = payload
            if predicate(payload):
                return payload
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(
        f"Timed out waiting for {url}. last_payload={last_payload} last_error={last_error}"
    )


def wait_for_http_response(*, url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3):
                return
        except (URLError, OSError) as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {url}. last_error={last_error}")


def start_process(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> tuple[subprocess.Popen, object]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return process, log_file


def terminate_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def maybe_generate_input_wav(input_path: Path) -> None:
    if input_path.exists():
        return
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "generate_test_wav.py"),
            "--output",
            str(input_path),
            "--duration-seconds",
            "0.8",
        ],
        check=True,
        cwd=REPO_ROOT,
    )


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else REPO_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    input_path = args.input if args.input is None or args.input.is_absolute() else REPO_ROOT / args.input
    if input_path is None:
        input_path = output_dir / "input.wav"
    maybe_generate_input_wav(input_path)

    worker_state_path = output_dir / "livekit-worker-state.json"
    check_json = output_dir / "check.json"
    capture_json = output_dir / "capture.json"
    capture_wav = output_dir / "capture.wav"
    analysis_json = output_dir / "analysis.json"

    common_env = os.environ.copy()
    common_env.update(
        {
            "LIVEKIT_URL": args.livekit_url,
            "LIVEKIT_API_KEY": args.livekit_api_key,
            "LIVEKIT_API_SECRET": args.livekit_api_secret,
            "LIVEKIT_ROOM": args.livekit_room,
            "LIVEKIT_AGENT_ID": args.livekit_agent_id,
            "QWEN_REALTIME_URL": f"ws://{args.stub_host}:{args.stub_port}/v1/realtime",
            "QWEN_CHAT_URL": f"http://{args.stub_host}:{args.stub_port}/v1/chat/completions",
            "QWEN_HEALTH_URL": f"http://{args.stub_host}:{args.stub_port}/health",
            "QWEN_AUDIO_BACKEND": "realtime",
            "QWEN_PREWARM_ON_STARTUP": "true",
            "QWEN_PREWARM_MAX_WAIT_SECONDS": "30",
            "QWEN_PREWARM_RETRY_INTERVAL_SECONDS": "1",
            "GATEWAY_HOST": args.gateway_host,
            "GATEWAY_PORT": str(args.gateway_port),
            "WEB_PORT": str(args.web_port),
            "VITE_GATEWAY_HTTP_URL": f"http://{args.gateway_host}:{args.gateway_port}",
            "VITE_DEV_PROXY_HTTP_TARGET": f"http://{args.gateway_host}:{args.gateway_port}",
            "LIVEKIT_WORKER_STATE_PATH": str(worker_state_path),
        }
    )

    stub_process = gateway_process = worker_process = web_process = None
    stub_log = gateway_log = worker_log = web_log = None
    try:
        stub_process, stub_log = start_process(
            name="stub_qwen",
            command=[
                sys.executable,
                str(REPO_ROOT / "scripts" / "stub_qwen_server.py"),
                "--host",
                args.stub_host,
                "--port",
                str(args.stub_port),
            ],
            cwd=REPO_ROOT,
            env=common_env,
            log_path=logs_dir / "stub-qwen.log",
        )
        wait_for_http_json(
            url=f"http://{args.stub_host}:{args.stub_port}/health",
            timeout_seconds=args.timeout_seconds,
            predicate=lambda payload: payload.get("status") == "ok",
        )

        gateway_process, gateway_log = start_process(
            name="gateway",
            command=[
                sys.executable,
                "-m",
                "uvicorn",
                "src.main:app",
                "--host",
                args.gateway_host,
                "--port",
                str(args.gateway_port),
            ],
            cwd=GATEWAY_ROOT,
            env=common_env,
            log_path=logs_dir / "gateway.log",
        )
        worker_process, worker_log = start_process(
            name="worker",
            command=[
                sys.executable,
                "-m",
                "src.livekit.worker",
            ],
            cwd=GATEWAY_ROOT,
            env=common_env,
            log_path=logs_dir / "worker.log",
        )
        web_process, web_log = start_process(
            name="web",
            command=[
                "npm.cmd",
                "run",
                "dev",
                "--",
                "--host",
                args.web_host,
                "--port",
                str(args.web_port),
            ],
            cwd=WEB_ROOT,
            env=common_env,
            log_path=logs_dir / "web.log",
        )

        ready_payload = wait_for_http_json(
            url=f"http://{args.gateway_host}:{args.gateway_port}/ready",
            timeout_seconds=args.timeout_seconds,
            predicate=lambda payload: payload.get("status") == "ready",
        )
        web_url = f"http://{args.web_host}:{args.web_port}/?transportDebug=1"
        wait_for_http_response(
            url=f"http://{args.web_host}:{args.web_port}",
            timeout_seconds=args.timeout_seconds,
        )

        subprocess.run(
            [
                "node",
                str(WEB_ROOT / "scripts" / "playwright-livekit-check.mjs"),
                web_url,
                str(input_path),
                str(check_json),
            ],
            check=True,
            cwd=WEB_ROOT,
        )
        subprocess.run(
            [
                "node",
                str(WEB_ROOT / "scripts" / "playwright-livekit-capture.mjs"),
                web_url,
                str(input_path),
                str(capture_json),
                str(capture_wav),
            ],
            check=True,
            cwd=WEB_ROOT,
        )
        subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "analyze_capture_audio.py"),
                str(capture_wav),
                "--output-json",
                str(analysis_json),
            ],
            check=True,
            cwd=REPO_ROOT,
        )

        summary = {
            "gateway_ready": ready_payload,
            "web_url": web_url,
            "input_wav": str(input_path),
            "check_json": str(check_json),
            "capture_json": str(capture_json),
            "capture_wav": str(capture_wav),
            "analysis_json": str(analysis_json),
            "logs_dir": str(logs_dir),
        }
        print(json.dumps(summary, indent=2), flush=True)
    finally:
        terminate_process(web_process)
        terminate_process(worker_process)
        terminate_process(gateway_process)
        terminate_process(stub_process)
        for log_file in (stub_log, gateway_log, worker_log, web_log):
            if log_file is not None:
                log_file.close()


if __name__ == "__main__":
    main()

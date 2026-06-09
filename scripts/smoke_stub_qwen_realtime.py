#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Start the local stub Qwen server, generate or reuse a 16 kHz WAV input, "
            "and run the direct realtime smoke test against it."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17091)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--chunk-ms", type=int, default=20)
    parser.add_argument("--wait-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--output-dir", type=Path, default=Path("tmp/stub-qwen-smoke"))
    return parser.parse_args()


def wait_for_health(*, host: str, port: int, timeout_seconds: float) -> None:
    health_url = f"http://{host}:{port}/health"
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(health_url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "ok":
                return
        except (URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for stub Qwen health at {health_url}: {last_error}")


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else REPO_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    input_path = args.input if args.input is None or args.input.is_absolute() else REPO_ROOT / args.input
    if input_path is None:
        input_path = output_dir / "input.wav"
        subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "generate_test_wav.py"),
                "--output",
                str(input_path),
                "--duration-seconds",
                "0.6",
            ],
            check=True,
            cwd=REPO_ROOT,
        )

    if not input_path.exists():
        raise SystemExit(f"Input WAV not found: {input_path}")

    stub_command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "stub_qwen_server.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    stub_process = subprocess.Popen(
        stub_command,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_health(
            host=args.host,
            port=args.port,
            timeout_seconds=args.wait_timeout_seconds,
        )
        smoke_command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "smoke_realtime_wav.py"),
            "--input",
            str(input_path),
            "--chunk-ms",
            str(args.chunk_ms),
            "--qwen-url",
            f"ws://{args.host}:{args.port}/v1/realtime",
            "--output",
            str(output_dir / "assistant-output.wav"),
            "--events",
            str(output_dir / "events.json"),
            "--metrics",
            str(output_dir / "metrics.json"),
        ]
        subprocess.run(smoke_command, check=True, cwd=REPO_ROOT)
    finally:
        stub_process.terminate()
        try:
            stub_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stub_process.kill()
            stub_process.wait(timeout=5)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

from __future__ import annotations

import argparse
from pathlib import Path


ORIGINAL = """    if engine_client is not None and getattr(engine_client, "async_chunk", False):
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "error": (
                    "The /v1/realtime API is not supported when async_chunk is enabled on the server. "
                    "Use a stage configuration with async_chunk disabled and restart the server before using "
                    "this endpoint."
                ),
                "code": "unsupported",
            }
        )
        await websocket.close()
        return
"""

PATCHED = """    allow_async_realtime = os.environ.get("VLLM_OMNI_ALLOW_REALTIME_ASYNC_CHUNK") == "1"
    if (
        engine_client is not None
        and getattr(engine_client, "async_chunk", False)
        and not allow_async_realtime
    ):
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "error": (
                    "The /v1/realtime API is not supported when async_chunk is enabled on the server. "
                    "Use a stage configuration with async_chunk disabled and restart the server before using "
                    "this endpoint."
                ),
                "code": "unsupported",
            }
        )
        await websocket.close()
        return
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Patch the local vllm-omni realtime route to allow async_chunk behind an env flag."
    )
    parser.add_argument(
        "--venv-path",
        type=Path,
        default=Path(".venv-qwen"),
        help="Path to the Qwen virtualenv root.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_server = (
        args.venv_path
        / "lib"
        / "python3.12"
        / "site-packages"
        / "vllm_omni"
        / "entrypoints"
        / "openai"
        / "api_server.py"
    )
    if not api_server.exists():
        raise SystemExit(f"error: api_server.py not found at {api_server}")

    current = api_server.read_text(encoding="utf-8")
    if 'VLLM_OMNI_ALLOW_REALTIME_ASYNC_CHUNK' in current:
        print(f"already patched: {api_server}")
        return
    if ORIGINAL not in current:
        raise SystemExit("error: expected realtime async guard block was not found")

    api_server.write_text(current.replace(ORIGINAL, PATCHED, 1), encoding="utf-8")
    print(f"patched: {api_server}")


if __name__ == "__main__":
    main()

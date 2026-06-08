#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "gateway"))

from livekit import rtc  # noqa: E402

from src.config import Settings  # noqa: E402
from src.livekit.auth import build_browser_token, build_worker_token  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify that a participant can connect to LiveKit.")
    parser.add_argument("--worker", action="store_true", help="Use the worker identity and token grants.")
    parser.add_argument("--identity", default=os.getenv("LIVEKIT_SMOKE_IDENTITY", "smoke-browser"))
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    settings = Settings()
    room = rtc.Room()

    identity = settings.livekit_agent_id if args.worker else args.identity
    token = build_worker_token(settings) if args.worker else build_browser_token(settings, identity=identity)

    print(
        "connecting "
        f"livekit_url={settings.livekit_url} "
        f"api_key_present={bool(settings.livekit_api_key)} "
        f"api_secret_present={bool(settings.livekit_api_secret)} "
        f"room={settings.livekit_room} "
        f"identity={identity}"
    )
    try:
        await room.connect(settings.livekit_url, token)
    except Exception as exc:
        print(f"failed livekit_url={settings.livekit_url} room={settings.livekit_room} error={exc}")
        raise
    try:
        print(
            f"connected livekit_url={settings.livekit_url} room={settings.livekit_room} identity={identity}"
        )
    finally:
        await room.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
    # `livekit-rtc` can panic in Rust during interpreter teardown even after a
    # successful connect/disconnect cycle. Exit the process immediately once the
    # smoke check has completed so the helper returns a stable status code.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)

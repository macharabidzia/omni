from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class LiveKitWorkerStateStore:
    def __init__(self, *, path: str) -> None:
        self.path = Path(path)
        self.snapshot: dict[str, Any] = {
            "livekit_worker_status": "starting",
            "livekit_room_joined": False,
            "livekit_input_track_status": "idle",
            "livekit_output_track_status": "missing",
            "livekit_reconnects": 0,
            "qwen_reconnects": 0,
            "qwen_reconnect_reasons": {},
            "qwen_last_reconnect_reason": None,
            "active_sessions": 0,
            "assistant_queue_depth_ms": 0.0,
            "error_count": 0,
            "interruption_count": 0,
            "duplicate_commit_count": 0,
            "stale_output_drop_count": 0,
            "metric_rollups": {},
        }
        self._flush()

    def update(self, **fields: object) -> None:
        changed = False
        for key, value in fields.items():
            if self.snapshot.get(key) == value:
                continue
            self.snapshot[key] = value
            changed = True
        if changed:
            self._flush()

    def record_metric_rollups(
        self,
        *,
        rollups: dict[str, float | int],
        assistant_queue_depth_ms: float,
        interrupt_clear_ms: float | None,
    ) -> None:
        self.update(
            metric_rollups=dict(rollups),
            assistant_queue_depth_ms=round(assistant_queue_depth_ms, 2),
            interrupt_clear_ms=interrupt_clear_ms,
        )

    def increment_counter(self, name: str, *, amount: int = 1) -> None:
        current_value = self.snapshot.get(name, 0)
        if not isinstance(current_value, int) or isinstance(current_value, bool):
            current_value = 0
        self.update(**{name: current_value + amount})

    def _flush(self) -> None:
        self.snapshot["updated_at_epoch_ms"] = int(time.time() * 1000)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
            tmp_path.write_text(
                json.dumps(self.snapshot, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp_path.replace(self.path)
        except Exception:
            logger.exception("Failed to write LiveKit worker state snapshot path=%s", self.path)


def load_livekit_worker_state(path: str) -> dict[str, object] | None:
    snapshot_path = Path(path)
    if not snapshot_path.exists():
        return None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None

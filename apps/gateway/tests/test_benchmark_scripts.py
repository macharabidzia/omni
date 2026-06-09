from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def test_benchmark_qwen_ttfb_script_runs_with_stub(tmp_path) -> None:
    output_json = tmp_path / "qwen-ttfb.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "benchmark_qwen_ttfb.py"),
            "--runs",
            "3",
            "--warmup-runs",
            "1",
            "--stub-first-audio-delay-ms",
            "5",
            "--stub-done-delay-ms",
            "5",
            "--first-audio-threshold-ms",
            "250",
            "--output-json",
            str(output_json),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(output_json.read_text(encoding="utf-8"))

    assert completed.returncode == 0
    assert payload["mode"] == "realtime_stub"
    assert payload["first_audio_threshold_ms"] == 250
    assert payload["aggregate"]["commit_to_first_audio_p95_ms"] is not None
    assert payload["aggregate"]["commit_to_first_audio_p95_ms"] <= 250
    assert len(payload["measurements"]) == 3


def test_benchmark_livekit_egress_script_runs(tmp_path) -> None:
    output_json = tmp_path / "livekit-egress.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "benchmark_livekit_egress.py"),
            "--runs",
            "3",
            "--warmup-runs",
            "1",
            "--first-frame-threshold-ms",
            "30",
            "--output-json",
            str(output_json),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(output_json.read_text(encoding="utf-8"))

    assert completed.returncode == 0
    assert payload["aggregate"]["first_capture_p95_ms"] is not None
    assert payload["aggregate"]["first_capture_p95_ms"] <= 30
    assert payload["aggregate"]["average_published_frames"] is not None
    assert len(payload["measurements"]) == 3


def test_benchmark_realtime_turn_script_runs_with_stub_backend(tmp_path) -> None:
    output_json = tmp_path / "realtime-turn.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "benchmark_realtime_turn.py"),
            "--runs",
            "2",
            "--warmup-runs",
            "1",
            "--stub-first-audio-delay-ms",
            "5",
            "--stub-done-delay-ms",
            "5",
            "--require-p95-ms",
            "500",
            "--output-json",
            str(output_json),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(output_json.read_text(encoding="utf-8"))

    assert completed.returncode == 0
    assert payload["mode"] == "in_process_stub_turn"
    assert payload["aggregate"]["speech_end_to_first_assistant_egress_p95_ms"] is not None
    assert payload["aggregate"]["speech_end_to_first_assistant_egress_p95_ms"] <= 500
    assert len(payload["turns"]) == 2


def test_smoke_stub_qwen_realtime_script_runs(tmp_path) -> None:
    output_dir = tmp_path / "stub-qwen-smoke"

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "smoke_stub_qwen_realtime.py"),
            "--port",
            "17191",
            "--output-dir",
            str(output_dir),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    metrics_payload = json.loads((output_dir / "metrics.json").read_text(encoding="utf-8"))
    events_payload = json.loads((output_dir / "events.json").read_text(encoding="utf-8"))

    assert completed.returncode == 0
    assert (output_dir / "assistant-output.wav").exists()
    assert metrics_payload["metrics"]["commit_to_first_audio_delta_ms"] is not None
    assert any(event["type"] == "assistant.done" for event in events_payload)

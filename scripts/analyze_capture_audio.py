#!/usr/bin/env python3
from __future__ import annotations

import argparse
import array
import json
import subprocess
import tempfile
import wave
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze captured assistant audio for gaps and clipping.")
    parser.add_argument("input", type=Path, help="Path to a WAV or decodable media file.")
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--expected-sample-rate", type=int, default=48000)
    parser.add_argument("--active-threshold", type=int, default=24)
    parser.add_argument("--silence-threshold", type=int, default=8)
    parser.add_argument("--max-internal-silence-ms", type=float, default=20.0)
    parser.add_argument(
        "--edge-padding-ms",
        type=float,
        default=60.0,
        help="Ignore silence runs within this distance of the active-region edges when classifying internal gaps.",
    )
    parser.add_argument(
        "--context-window-ms",
        type=float,
        default=10.0,
        help="Inspect this much audio on each side of an internal silence run before classifying it as a suspicious gap.",
    )
    parser.add_argument(
        "--context-active-threshold",
        type=int,
        default=32,
        help="Treat an internal silence run as suspicious only when the surrounding context exceeds this amplitude threshold.",
    )
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    analysis = analyze_audio(
        args.input,
        expected_sample_rate=args.expected_sample_rate,
        frame_ms=args.frame_ms,
        active_threshold=args.active_threshold,
        silence_threshold=args.silence_threshold,
        max_internal_silence_ms=args.max_internal_silence_ms,
        edge_padding_ms=args.edge_padding_ms,
        context_window_ms=args.context_window_ms,
        context_active_threshold=args.context_active_threshold,
    )
    payload = json.dumps(analysis, indent=2)
    if args.output_json is not None:
        args.output_json.write_text(payload + "\n", encoding="utf-8")
    print(payload)

    if analysis["status"] != "ok":
        raise SystemExit(1)


def analyze_audio(
    input_path: Path,
    *,
    expected_sample_rate: int,
    frame_ms: int,
    active_threshold: int,
    silence_threshold: int,
    max_internal_silence_ms: float,
    edge_padding_ms: float,
    context_window_ms: float,
    context_active_threshold: int,
) -> dict[str, object]:
    wav_path = decode_to_wav_if_needed(input_path, expected_sample_rate=expected_sample_rate)
    try:
        sample_rate, samples = load_pcm16_mono_wav(wav_path)
    finally:
        if wav_path != input_path:
            wav_path.unlink(missing_ok=True)

    analysis = analyze_samples(
        samples,
        sample_rate=sample_rate,
        input_path=input_path,
        expected_sample_rate=expected_sample_rate,
        frame_ms=frame_ms,
        active_threshold=active_threshold,
        silence_threshold=silence_threshold,
        max_internal_silence_ms=max_internal_silence_ms,
        edge_padding_ms=edge_padding_ms,
        context_window_ms=context_window_ms,
        context_active_threshold=context_active_threshold,
    )
    return analysis


def analyze_samples(
    samples: array.array,
    *,
    sample_rate: int,
    input_path: Path,
    expected_sample_rate: int,
    frame_ms: int,
    active_threshold: int,
    silence_threshold: int,
    max_internal_silence_ms: float,
    edge_padding_ms: float,
    context_window_ms: float,
    context_active_threshold: int,
) -> dict[str, object]:
    if samples.typecode != "h":
        raise ValueError("Audio analysis requires PCM16 samples.")

    active_start, active_end = find_active_region(samples, threshold=active_threshold)
    active_samples = samples[active_start : active_end + 1] if active_end >= active_start else array.array("h")
    frame_samples = max(1, int(sample_rate * frame_ms / 1000))
    boundary_absdiff = [
        abs(active_samples[index] - active_samples[index - 1])
        for index in range(frame_samples, len(active_samples), frame_samples)
    ]
    silence_runs = find_silence_runs(
        active_samples,
        threshold=silence_threshold,
        min_run_samples=max(1, int(sample_rate * max_internal_silence_ms / 1000)),
    )
    edge_padding_samples = max(0, int(sample_rate * edge_padding_ms / 1000))
    edge_silence_runs = [
        silence_run
        for silence_run in silence_runs
        if silence_run[0] < edge_padding_samples
        or len(active_samples) - silence_run[1] - 1 < edge_padding_samples
    ]
    internal_silence_runs = [
        silence_run for silence_run in silence_runs if silence_run not in edge_silence_runs
    ]
    suspicious_internal_silence_runs = find_suspicious_internal_silence_runs(
        active_samples,
        internal_silence_runs=internal_silence_runs,
        context_window_samples=max(1, int(sample_rate * context_window_ms / 1000)),
        context_active_threshold=context_active_threshold,
    )
    clipped_samples = sum(1 for sample in active_samples if abs(sample) >= 32760)
    longest_internal_silence_ms = (
        round(max(length for _, _, length in internal_silence_runs) * 1000 / sample_rate, 2)
        if internal_silence_runs
        else 0.0
    )
    longest_edge_silence_ms = (
        round(max(length for _, _, length in edge_silence_runs) * 1000 / sample_rate, 2)
        if edge_silence_runs
        else 0.0
    )
    longest_suspicious_internal_silence_ms = (
        round(max(length for _, _, length in suspicious_internal_silence_runs) * 1000 / sample_rate, 2)
        if suspicious_internal_silence_runs
        else 0.0
    )

    warnings: list[str] = []
    if sample_rate != expected_sample_rate:
        warnings.append(
            f"sample rate {sample_rate} did not match expected {expected_sample_rate}"
        )
    if clipped_samples > 0:
        warnings.append(f"{clipped_samples} clipped samples detected")
    if longest_suspicious_internal_silence_ms > max_internal_silence_ms:
        warnings.append(
            "suspicious internal silence run "
            f"{longest_suspicious_internal_silence_ms} ms exceeded {max_internal_silence_ms} ms"
        )

    status = "ok" if not warnings else "warning"
    return {
        "status": status,
        "input_path": str(input_path),
        "sample_rate": sample_rate,
        "duration_ms": round(len(samples) * 1000 / sample_rate, 2),
        "active_region": {
            "start_ms": round(active_start * 1000 / sample_rate, 2),
            "end_ms": round(active_end * 1000 / sample_rate, 2),
            "duration_ms": round(len(active_samples) * 1000 / sample_rate, 2),
        },
        "edge_padding_ms": edge_padding_ms,
        "context_window_ms": context_window_ms,
        "context_active_threshold": context_active_threshold,
        "edge_silence_runs_ge_threshold": len(edge_silence_runs),
        "longest_edge_silence_ms": longest_edge_silence_ms,
        "edge_silence_runs": describe_silence_runs(
            edge_silence_runs,
            active_samples=active_samples,
            sample_rate=sample_rate,
            active_start=active_start,
            context_window_samples=max(1, int(sample_rate * context_window_ms / 1000)),
        ),
        "internal_silence_runs_ge_threshold": len(internal_silence_runs),
        "longest_internal_silence_ms": longest_internal_silence_ms,
        "internal_silence_runs": describe_silence_runs(
            internal_silence_runs,
            active_samples=active_samples,
            sample_rate=sample_rate,
            active_start=active_start,
            context_window_samples=max(1, int(sample_rate * context_window_ms / 1000)),
        ),
        "suspicious_internal_silence_runs_ge_threshold": len(suspicious_internal_silence_runs),
        "longest_suspicious_internal_silence_ms": longest_suspicious_internal_silence_ms,
        "suspicious_internal_silence_runs": describe_silence_runs(
            suspicious_internal_silence_runs,
            active_samples=active_samples,
            sample_rate=sample_rate,
            active_start=active_start,
            context_window_samples=max(1, int(sample_rate * context_window_ms / 1000)),
        ),
        "clipped_samples": clipped_samples,
        "boundary_absdiff": {
            "count": len(boundary_absdiff),
            "max": max(boundary_absdiff) if boundary_absdiff else 0,
            "p95": percentile(boundary_absdiff, 95),
        },
        "warnings": warnings,
    }


def decode_to_wav_if_needed(input_path: Path, *, expected_sample_rate: int) -> Path:
    if input_path.suffix.lower() == ".wav":
        return input_path

    temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    temp_path = Path(temp_file.name)
    temp_file.close()
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-ac",
            "1",
            "-ar",
            str(expected_sample_rate),
            str(temp_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return temp_path


def load_pcm16_mono_wav(path: Path) -> tuple[int, array.array]:
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getnchannels() != 1:
            raise ValueError("Audio analysis requires mono WAV input.")
        if wav_file.getsampwidth() != 2:
            raise ValueError("Audio analysis requires PCM16 WAV input.")
        sample_rate = wav_file.getframerate()
        data = wav_file.readframes(wav_file.getnframes())

    samples = array.array("h")
    samples.frombytes(data)
    return sample_rate, samples


def find_active_region(samples: array.array, *, threshold: int) -> tuple[int, int]:
    start = 0
    while start < len(samples) and abs(samples[start]) <= threshold:
        start += 1
    if start == len(samples):
        return 0, -1

    end = len(samples) - 1
    while end > start and abs(samples[end]) <= threshold:
        end -= 1
    return start, end


def find_silence_runs(
    samples: array.array,
    *,
    threshold: int,
    min_run_samples: int,
) -> list[tuple[int, int, int]]:
    runs: list[tuple[int, int, int]] = []
    run = 0
    run_start = 0
    for sample_index, sample in enumerate(samples):
        if abs(sample) <= threshold:
            if run == 0:
                run_start = sample_index
            run += 1
            continue
        if run >= min_run_samples:
            runs.append((run_start, sample_index - 1, run))
        run = 0
    if run >= min_run_samples:
        runs.append((run_start, run_start + run - 1, run))
    return runs


def find_suspicious_internal_silence_runs(
    samples: array.array,
    *,
    internal_silence_runs: list[tuple[int, int, int]],
    context_window_samples: int,
    context_active_threshold: int,
) -> list[tuple[int, int, int]]:
    suspicious_runs: list[tuple[int, int, int]] = []
    for silence_run in internal_silence_runs:
        start, end, _ = silence_run
        pre_context = samples[max(0, start - context_window_samples) : start]
        post_context = samples[end + 1 : min(len(samples), end + 1 + context_window_samples)]
        pre_peak = max((abs(sample) for sample in pre_context), default=0)
        post_peak = max((abs(sample) for sample in post_context), default=0)
        if pre_peak >= context_active_threshold and post_peak >= context_active_threshold:
            suspicious_runs.append(silence_run)
    return suspicious_runs


def describe_silence_runs(
    silence_runs: list[tuple[int, int, int]],
    *,
    active_samples: array.array,
    sample_rate: int,
    active_start: int,
    context_window_samples: int,
) -> list[dict[str, float | int]]:
    described: list[dict[str, float | int]] = []
    for start, end, length in silence_runs:
        before = active_samples[max(0, start - context_window_samples) : start]
        after = active_samples[end + 1 : min(len(active_samples), end + 1 + context_window_samples)]
        described.append(
            {
                "start_ms_in_active": round(start * 1000 / sample_rate, 2),
                "end_ms_in_active": round(end * 1000 / sample_rate, 2),
                "length_ms": round(length * 1000 / sample_rate, 2),
                "start_ms_absolute": round((active_start + start) * 1000 / sample_rate, 2),
                "end_ms_absolute": round((active_start + end) * 1000 / sample_rate, 2),
                "pre_peak_abs": max((abs(sample) for sample in before), default=0),
                "post_peak_abs": max((abs(sample) for sample in after), default=0),
            }
        )
    return described


def percentile(values: list[int], pct: int) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct / 100))
    return ordered[index]


if __name__ == "__main__":
    main()

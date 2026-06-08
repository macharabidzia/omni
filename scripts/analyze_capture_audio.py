#!/usr/bin/env python3
from __future__ import annotations

import argparse
import array
import json
import math
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
    parser.add_argument(
        "--voiced-rms-threshold",
        type=float,
        default=200.0,
        help="Ignore low-energy frames when classifying rough/noisy playback artifacts.",
    )
    parser.add_argument(
        "--roughness-threshold",
        type=float,
        default=1.0,
        help="Flag voiced frames whose mean absolute sample delta exceeds this multiple of RMS.",
    )
    parser.add_argument(
        "--zero-crossing-threshold",
        type=float,
        default=0.35,
        help="Flag voiced frames whose zero-crossing ratio exceeds this threshold.",
    )
    parser.add_argument(
        "--max-noisy-frame-ratio",
        type=float,
        default=0.12,
        help="Warn when rough/noisy voiced frames exceed this fraction of voiced frames.",
    )
    parser.add_argument(
        "--click-derivative-threshold",
        type=float,
        default=12000.0,
        help="Minimum second-derivative magnitude required to count a click-like spike.",
    )
    parser.add_argument(
        "--click-rms-multiplier",
        type=float,
        default=8.0,
        help="Scale click detection relative to the active-region RMS.",
    )
    parser.add_argument(
        "--max-click-spikes-per-second",
        type=float,
        default=0.5,
        help="Warn when click-like sample spikes exceed this rate.",
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
        voiced_rms_threshold=args.voiced_rms_threshold,
        roughness_threshold=args.roughness_threshold,
        zero_crossing_threshold=args.zero_crossing_threshold,
        max_noisy_frame_ratio=args.max_noisy_frame_ratio,
        click_derivative_threshold=args.click_derivative_threshold,
        click_rms_multiplier=args.click_rms_multiplier,
        max_click_spikes_per_second=args.max_click_spikes_per_second,
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
    voiced_rms_threshold: float = 200.0,
    roughness_threshold: float = 1.0,
    zero_crossing_threshold: float = 0.35,
    max_noisy_frame_ratio: float = 0.12,
    click_derivative_threshold: float = 12000.0,
    click_rms_multiplier: float = 8.0,
    max_click_spikes_per_second: float = 0.5,
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
        voiced_rms_threshold=voiced_rms_threshold,
        roughness_threshold=roughness_threshold,
        zero_crossing_threshold=zero_crossing_threshold,
        max_noisy_frame_ratio=max_noisy_frame_ratio,
        click_derivative_threshold=click_derivative_threshold,
        click_rms_multiplier=click_rms_multiplier,
        max_click_spikes_per_second=max_click_spikes_per_second,
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
    voiced_rms_threshold: float = 200.0,
    roughness_threshold: float = 1.0,
    zero_crossing_threshold: float = 0.35,
    max_noisy_frame_ratio: float = 0.12,
    click_derivative_threshold: float = 12000.0,
    click_rms_multiplier: float = 8.0,
    max_click_spikes_per_second: float = 0.5,
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
    frame_artifacts = analyze_frame_artifacts(
        active_samples,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        voiced_rms_threshold=voiced_rms_threshold,
        roughness_threshold=roughness_threshold,
        zero_crossing_threshold=zero_crossing_threshold,
    )
    click_spikes = analyze_click_spikes(
        active_samples,
        sample_rate=sample_rate,
        click_derivative_threshold=click_derivative_threshold,
        click_rms_multiplier=click_rms_multiplier,
    )
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
    if frame_artifacts["noisy_frame_ratio"] > max_noisy_frame_ratio:
        warnings.append(
            "rough/noisy voiced frame ratio "
            f"{round(frame_artifacts['noisy_frame_ratio'], 4)} exceeded {max_noisy_frame_ratio}"
        )
    if click_spikes["per_second"] > max_click_spikes_per_second:
        warnings.append(
            "click-like spike rate "
            f"{round(click_spikes['per_second'], 2)} per second exceeded {max_click_spikes_per_second}"
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
        "roughness": {
            "frame_ms": frame_ms,
            "voiced_rms_threshold": voiced_rms_threshold,
            "threshold": roughness_threshold,
            "mean": round(frame_artifacts["roughness_mean"], 4),
            "p95": round(frame_artifacts["roughness_p95"], 4),
            "max": round(frame_artifacts["roughness_max"], 4),
        },
        "zero_crossing_ratio": {
            "frame_ms": frame_ms,
            "threshold": zero_crossing_threshold,
            "mean": round(frame_artifacts["zero_crossing_mean"], 4),
            "p95": round(frame_artifacts["zero_crossing_p95"], 4),
            "max": round(frame_artifacts["zero_crossing_max"], 4),
        },
        "noisy_frames": {
            "count": frame_artifacts["noisy_frame_count"],
            "voiced_frame_count": frame_artifacts["voiced_frame_count"],
            "ratio": round(frame_artifacts["noisy_frame_ratio"], 4),
            "roughness_threshold": roughness_threshold,
            "zero_crossing_threshold": zero_crossing_threshold,
        },
        "click_spikes": {
            "count": click_spikes["count"],
            "per_second": round(click_spikes["per_second"], 4),
            "max_second_derivative": click_spikes["max_second_derivative"],
            "derivative_threshold": round(click_spikes["threshold"], 2),
            "base_derivative_threshold": click_derivative_threshold,
            "rms_multiplier": click_rms_multiplier,
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


def analyze_frame_artifacts(
    samples: array.array,
    *,
    sample_rate: int,
    frame_ms: int,
    voiced_rms_threshold: float,
    roughness_threshold: float,
    zero_crossing_threshold: float,
) -> dict[str, float | int]:
    frame_samples = max(1, int(sample_rate * frame_ms / 1000))
    hop_samples = max(1, frame_samples // 2)
    roughness_values: list[float] = []
    zero_crossing_values: list[float] = []
    voiced_frame_count = 0
    noisy_frame_count = 0

    for start in range(0, max(0, len(samples) - frame_samples + 1), hop_samples):
        frame = samples[start : start + frame_samples]
        if len(frame) < frame_samples:
            continue
        rms = frame_rms(frame)
        if rms < voiced_rms_threshold:
            continue
        voiced_frame_count += 1
        roughness = frame_roughness(frame, rms=rms)
        zero_crossing_ratio = frame_zero_crossing_ratio(frame)
        roughness_values.append(roughness)
        zero_crossing_values.append(zero_crossing_ratio)
        if roughness >= roughness_threshold and zero_crossing_ratio >= zero_crossing_threshold:
            noisy_frame_count += 1

    noisy_frame_ratio = (
        noisy_frame_count / voiced_frame_count if voiced_frame_count > 0 else 0.0
    )
    return {
        "roughness_mean": mean(roughness_values),
        "roughness_p95": percentile_float(roughness_values, 95),
        "roughness_max": max(roughness_values, default=0.0),
        "zero_crossing_mean": mean(zero_crossing_values),
        "zero_crossing_p95": percentile_float(zero_crossing_values, 95),
        "zero_crossing_max": max(zero_crossing_values, default=0.0),
        "voiced_frame_count": voiced_frame_count,
        "noisy_frame_count": noisy_frame_count,
        "noisy_frame_ratio": noisy_frame_ratio,
    }


def analyze_click_spikes(
    samples: array.array,
    *,
    sample_rate: int,
    click_derivative_threshold: float,
    click_rms_multiplier: float,
) -> dict[str, float | int]:
    if len(samples) < 3:
        return {
            "count": 0,
            "per_second": 0.0,
            "max_second_derivative": 0,
            "threshold": click_derivative_threshold,
        }

    rms = frame_rms(samples)
    threshold = max(click_derivative_threshold, rms * click_rms_multiplier)
    click_count = 0
    max_second_derivative = 0
    previous_sample = samples[0]
    current_sample = samples[1]
    for next_sample in samples[2:]:
        second_derivative = abs(next_sample - (2 * current_sample) + previous_sample)
        max_second_derivative = max(max_second_derivative, second_derivative)
        if second_derivative >= threshold:
            click_count += 1
        previous_sample = current_sample
        current_sample = next_sample

    duration_seconds = len(samples) / sample_rate if sample_rate > 0 else 0.0
    return {
        "count": click_count,
        "per_second": click_count / duration_seconds if duration_seconds > 0 else 0.0,
        "max_second_derivative": max_second_derivative,
        "threshold": threshold,
    }


def frame_rms(samples: array.array) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def frame_roughness(samples: array.array, *, rms: float) -> float:
    if len(samples) < 2:
        return 0.0
    mean_abs_delta = sum(
        abs(samples[index] - samples[index - 1]) for index in range(1, len(samples))
    ) / (len(samples) - 1)
    return mean_abs_delta / max(rms, 1.0)


def frame_zero_crossing_ratio(samples: array.array) -> float:
    if len(samples) < 2:
        return 0.0
    crossings = 0
    previous_sign = sign(samples[0])
    for sample in samples[1:]:
        current_sign = sign(sample)
        if current_sign != previous_sign:
            crossings += 1
        previous_sign = current_sign
    return crossings / (len(samples) - 1)


def sign(sample: int) -> int:
    return -1 if sample < 0 else 1


def mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def percentile_float(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct / 100))
    return ordered[index]


if __name__ == "__main__":
    main()

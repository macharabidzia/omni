#!/usr/bin/env python3
import argparse
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark first-audio latency across chunk sizes and speakers.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--direct", action="store_true")
    mode.add_argument("--gateway", action="store_true")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--chunk-ms-list", default="40,80,120,200")
    parser.add_argument("--speakers", default="Ethan,Chelsie,Aiden")
    parser.add_argument("--modalities-list", default="text;text+audio")
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results"))
    parser.add_argument("--smoke-script", type=Path, default=REPO_ROOT / "scripts" / "smoke_realtime_wav.py")
    return parser.parse_args()


def parse_modalities_list(raw: str) -> list[str]:
    values = []
    for item in raw.replace(";", ",").split(","):
        value = item.strip()
        if not value:
            continue
        if value == "text":
            values.append("text")
        elif value in {"text+audio", "text,audio"}:
            values.append("text,audio")
        else:
            raise ValueError(f"Unsupported modalities entry: {value}")
    return values


def percentile(values: list[float], pct: int) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    quantiles = statistics.quantiles(values, n=100, method="inclusive")
    return round(quantiles[pct - 1], 2)


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else REPO_ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "event_logs").mkdir(parents=True, exist_ok=True)

    chunk_sizes = [int(value.strip()) for value in args.chunk_ms_list.split(",") if value.strip()]
    speakers = [value.strip() for value in args.speakers.split(",") if value.strip()]
    modalities_list = parse_modalities_list(args.modalities_list)
    rows: list[dict] = []

    for chunk_ms in chunk_sizes:
        for speaker in speakers:
            for modalities in modalities_list:
                for run_index in range(1, args.runs + 1):
                    stem = f"chunk{chunk_ms}_{speaker}_{modalities.replace(',', '-')}_run{run_index}"
                    events_path = output_dir / "event_logs" / f"{stem}.json"
                    metrics_path = output_dir / "event_logs" / f"{stem}.metrics.json"
                    output_wav_path = output_dir / "event_logs" / f"{stem}.wav"

                    command = [
                        sys.executable,
                        str(args.smoke_script),
                        "--direct" if args.direct else "--gateway",
                        "--input",
                        str(args.input),
                        "--chunk-ms",
                        str(chunk_ms),
                        "--speaker",
                        speaker,
                        "--modalities",
                        modalities,
                        "--events",
                        str(events_path),
                        "--metrics",
                        str(metrics_path),
                        "--output",
                        str(output_wav_path),
                    ]
                    subprocess.run(command, check=True)

                    metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
                    row = {
                        "mode": "direct" if args.direct else "gateway",
                        "chunk_ms": chunk_ms,
                        "speaker": speaker,
                        "modalities": modalities,
                        "run": run_index,
                        "commit_to_first_transcript_ms": metrics_payload["metrics"]["commit_to_first_transcript_ms"],
                        "commit_to_first_text_ms": metrics_payload["metrics"]["commit_to_first_text_ms"],
                        "commit_to_first_audio_delta_ms": metrics_payload["metrics"]["commit_to_first_audio_delta_ms"],
                        "commit_to_first_audio_played_ms": metrics_payload["metrics"]["commit_to_first_audio_played_ms"],
                        "full_response_ms": metrics_payload["metrics"]["full_response_ms"],
                    }
                    rows.append(row)

    csv_path = output_dir / "first_audio.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    markdown_path = output_dir / "first_audio.md"
    lines = ["# First Audio Benchmark", "", "| Chunk | Speaker | Modalities | Runs | p50 first audio played | p95 first audio played |", "| --- | --- | --- | --- | --- | --- |"]

    for chunk_ms in chunk_sizes:
        for speaker in speakers:
            for modalities in modalities_list:
                combo_rows = [
                    row
                    for row in rows
                    if row["chunk_ms"] == chunk_ms
                    and row["speaker"] == speaker
                    and row["modalities"] == modalities
                ]
                first_audio_values = [
                    row["commit_to_first_audio_played_ms"]
                    for row in combo_rows
                    if isinstance(row["commit_to_first_audio_played_ms"], (int, float))
                ]
                p50 = percentile(first_audio_values, 50)
                p95 = percentile(first_audio_values, 95)
                lines.append(
                    f"| {chunk_ms} | {speaker} | {modalities} | {len(combo_rows)} | {p50} | {p95} |"
                )

    all_first_audio = [
        row["commit_to_first_audio_played_ms"]
        for row in rows
        if isinstance(row["commit_to_first_audio_played_ms"], (int, float))
    ]
    realistic = bool(all_first_audio and min(all_first_audio) <= 150.0)
    lines.extend(
        [
            "",
            f"150 ms realistic on this hardware: {'yes' if realistic else 'no'}",
            "",
            "This report uses the smoke-test event stream and treats first received audio as first playable audio.",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

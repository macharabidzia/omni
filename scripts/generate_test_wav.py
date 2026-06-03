#!/usr/bin/env python3
import argparse
import math
import wave
from pathlib import Path


def generate_sine_wave(
    *,
    output_path: Path,
    duration_seconds: float,
    sample_rate: int,
    frequency_hz: float,
    amplitude: float,
) -> None:
    frame_count = int(duration_seconds * sample_rate)
    pcm_frames = bytearray()

    for index in range(frame_count):
        sample = amplitude * math.sin(2 * math.pi * frequency_hz * (index / sample_rate))
        value = int(max(-1.0, min(1.0, sample)) * 32767)
        pcm_frames.extend(value.to_bytes(2, byteorder="little", signed=True))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(pcm_frames))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a 16 kHz mono PCM16 test WAV.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, default=2.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--frequency-hz", type=float, default=440.0)
    parser.add_argument("--amplitude", type=float, default=0.2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generate_sine_wave(
        output_path=args.output,
        duration_seconds=args.duration_seconds,
        sample_rate=args.sample_rate,
        frequency_hz=args.frequency_hz,
        amplitude=args.amplitude,
    )


if __name__ == "__main__":
    main()


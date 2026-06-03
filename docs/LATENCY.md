# Latency

This project tracks:

- `t_session_start`
- `t_microphone_started`
- `t_first_audio_chunk_sent`
- `t_audio_commit_sent`
- `t_first_transcript_delta`
- `t_first_text_delta`
- `t_first_audio_delta_received`
- `t_first_audio_played`
- `t_response_done`

Derived metrics:

- `mic_to_first_transcript_ms`
- `commit_to_first_text_ms`
- `commit_to_first_audio_delta_ms`
- `commit_to_first_audio_played_ms`
- `full_response_ms`

## Current State

Measured latency has not been recorded in this workspace yet because the Qwen3-Omni server has not been run here against the target GPU runtime.

Run:

```bash
python3 scripts/benchmark_first_audio.py --gateway --input /path/to/input.wav
```

Then update this document with the generated `benchmark-results/first_audio.csv` and `benchmark-results/first_audio.md`.


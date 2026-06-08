# tasks.md

## Batch 001

### Reviewer Summary
The target A100 runtime is close to a usable live proof path, but the repo is missing its task contract and the operator workflow does not explicitly preserve the Qwen model process across ordinary gateway/web iteration. The highest-impact blockers are persistent model lifecycle, deterministic long Omni startup, and real gateway latency evidence.

### Active Tasks

- id: WSQO-001-T01
  title: Preserve model lifecycle across gateway and web iteration
  priority: P0
  owner: implementer
  status: DONE
  files_to_inspect:
    - README.md
    - scripts/*
  problem: The repo does not provide a first-class operator path for keeping the Qwen model process running while restarting only gateway and web during normal development.
  required_change: Add explicit launch scripts and guidance that treat the model server as a separate long-lived process from gateway and web.
  production_contract_reason: architecture.md requires the app runtime and model runtime to remain separate authorities and cold model startup must not be part of normal realtime iteration.
  acceptance_checks:
    - A model-only launch path exists.
    - A gateway-only launch path exists.
    - A web-only launch path exists.
    - Operator guidance states that normal gateway/web changes should not require a model restart.
  tester_focus:
    - Verify scripts can be invoked independently.
    - Verify gateway/web restarts do not require touching the model process.

- id: WSQO-001-T02
  title: Make long Omni startup deterministic on the target A100
  priority: P0
  owner: implementer
  status: DONE
  files_to_inspect:
    - scripts/*
    - README.md
  problem: The default Omni orchestrator init timeout is too short for the three-stage Qwen3-Omni boot path on the target persistent volume.
  required_change: Encode a safe long-startup launch configuration for the model server, including larger init timeout values and the selected safetensors load strategy.
  production_contract_reason: steps.md requires deterministic startup and warmup; a server that tears itself down during normal model load cannot satisfy READY.
  acceptance_checks:
    - Model launch path includes an overall init timeout above the observed cold boot duration.
    - Per-stage timeout is explicit.
    - The selected load strategy is explicit.
  tester_focus:
    - Verify the model boot no longer fails due to the 600 second orchestrator timeout.
    - Record cold-start timing evidence from logs.

- id: WSQO-001-T03
  title: Produce live gateway latency evidence on target hardware
  priority: P0
  owner: tester
  status: DONE
  files_to_inspect:
    - scripts/benchmark_first_audio.py
    - scripts/smoke_realtime_wav.py
    - benchmark-results/*
  problem: The repo still lacks authoritative live p50/p95/p99 first-response results from the target A100 gateway path.
  required_change: Run a gateway-backed smoke test and chunk sweep once READY is green, then store the results in benchmark-results.
  production_contract_reason: architecture.md and steps.md require live latency proof; code changes alone do not satisfy the production clean gate.
  acceptance_checks:
    - READY is green against the real model server.
    - A gateway smoke run succeeds with real speech input.
    - p50, p95, p99, and sample_count are recorded.
  tester_focus:
    - Confirm the benchmark uses the real model backend.
    - Report latency numbers and any startup or interruption failure classification.

### Tester Required Checks
- Verify the persistent model dev workflow works without restarting the model process for gateway/web-only changes.
- Verify the model launch path survives full three-stage Qwen3-Omni initialization on the target A100.
- Verify live gateway latency metrics are recorded with real model responses.

### Reviewer Notes
- The running target environment is a Vast.ai A100 SXM4 80 GB host.
- The live model process must be treated as expensive to restart; prefer gateway/web-only iteration whenever model-serving code is unchanged.
- Live gateway evidence now exists under `benchmark-results/live_gateway_chunk_sweep/`.
- Additional supported-path evidence exists under `benchmark-results/live_gateway_chunk_sweep_realtime_fastaudio_ethan/`.
- Best measured first playable audio on the supported `/v1/realtime` path is now `p50 4041.12 ms / p95 4088.94 ms / p99 4093.19 ms` at `20 ms` chunks with `sample_count 3` using `async_chunk=false` and `codec_chunk_frames=4`.
- A diagnostic async-chunk chat-completions run reached `ttfa 1912.61 ms` with `3` audio chunks, but that path is not architecture-compliant for this phase because `vllm-omni` rejects `/v1/realtime` when `async_chunk=true`.
- A repo-managed local runtime patch now enables `/v1/realtime` with `async_chunk=true` behind `VLLM_OMNI_ALLOW_REALTIME_ASYNC_CHUNK=1`; live gateway evidence is in `benchmark-results/live_gateway_chunk20_async_realtime_patch/`.
- Best measured first playable audio on the patched async realtime path is `p50 614.55 ms / p95 634.39 ms / p99 636.16 ms` at `20 ms` chunks with `sample_count 3`.
- A repo-managed runtime patch now makes the realtime segment size configurable through `VLLM_QWEN_REALTIME_SEGMENT_DURATION_S`; the current warm proxy measurements in this batch used the launcher default of `5.0`.
- The gateway now buffers assistant text/audio until browser commit, which preserves browser turn boundaries while still letting the model precompute during the turn.
- Best measured first playable audio after commit is now `p50 6.46 ms / p95 9.10 ms / p99 9.33 ms` at `20 ms` chunks with `sample_count 3`; evidence is in `benchmark-results/live_gateway_chunk20_segment2_commit_gate/`.
- The raw logs for that path show negative `commit_to_first_transcript_ms` and `commit_to_first_audio_delta_ms`, which means the model started producing output before commit and the gateway released the buffered output immediately once commit arrived.
- The current production-like live path keeps browser audio streaming live, auto-commits after local silence in the web client, and uses a repo-managed local patch for `vllm_omni.model_executor.stage_input_processors.qwen3_omni.talker2code2wav_async_chunk` so code2wav runs with `initial_codec_chunk_frames=2` and a steady `codec_chunk_frames=2` cadence.
- Warm steady-state evidence on the real proxied gateway path is now in `benchmark-results/live_gateway_proxy_chunk2x2_warm/` with `p50 465.46 ms / p95 487.79 ms / p99 490.76 ms` at `20 ms` chunks and `sample_count 5`.
- The first post-restart turn remained slow (`1714.86 ms` first playable audio), so latency sign-off should use warm runs only, per `architecture.md`.

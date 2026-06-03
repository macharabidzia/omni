# Tasks

- [x] Phase 1: repository scaffolded and docs created
- [x] Phase 2: Docker Compose and Qwen Dockerfile added
- [x] Phase 3: gateway implementation
- [x] Phase 4: Qwen realtime websocket client
- [x] Phase 5: event normalization
- [x] Phase 6: audio validation
- [x] Phase 7: WAV smoke test
- [x] Phase 8: browser frontend
- [x] Phase 9: playback queue
- [x] Phase 10: metrics
- [x] Phase 11: speaker selection
- [x] Phase 12: text-only mode
- [x] Phase 13: barge-in
- [x] Phase 14: first-audio benchmark
- [ ] Phase 15: operations and latency docs verified with runtime data

## Current Verification

- gateway unit tests pass locally
- frontend production build passes locally
- gateway `/health`, `/ready`, and `/ws/realtime` were exercised locally
- direct `hf download` prefetch of 5 GB Qwen shards was verified locally through `scripts/prefetch_qwen_model.sh`
- a full end-to-end audio streaming run still depends on a live Qwen3-Omni runtime with the required GPU and Docker stack

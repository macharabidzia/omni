You are connecting my RunPod voice project to my self-hosted LiveKit server on Snel VPS.

LiveKit server:
- LIVEKIT_URL=ws://185.62.58.164:7880
- LIVEKIT_API_KEY=devkey
- LIVEKIT_API_SECRET=devsecret
- RTC UDP mux port is 7882
- TCP fallback port is 7881
- LiveKit is running on Snel VPS, not RunPod
- RunPod only runs AI runtime: ASR/LLM/TTS/backend worker

Goal:
Make the RunPod project connect to this LiveKit server as a LiveKit worker/agent or bridge.

Tasks:
1. Find where the project currently defines LiveKit config, environment variables, or connection settings.
2. Add or update environment variables:
   LIVEKIT_URL=ws://185.62.58.164:7880
   LIVEKIT_API_KEY=devkey
   LIVEKIT_API_SECRET=devsecret
3. If the project uses Python LiveKit Agents, make sure the worker reads LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET from env.
4. If the project uses a custom LiveKit bridge, make sure it connects to ws://185.62.58.164:7880 using API key devkey and secret devsecret.
5. Do not run LiveKit server inside RunPod. Only connect to the Snel LiveKit server.
6. Add a small startup validation that prints:
   - LiveKit URL being used
   - whether API key/secret are present
   - whether the worker connected or failed
7. Add a simple smoke test command or script that verifies the RunPod worker can connect to LiveKit.
8. Do not change ASR/LLM/TTS model logic yet. Only wire LiveKit connection cleanly.
9. If any old LiveKit URL points to localhost, LiveKit Cloud, or RunPod, replace it with the Snel URL.
10. After changes, show me the exact files changed and the exact command to run the worker.
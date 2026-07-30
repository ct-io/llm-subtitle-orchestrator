# LLM Subtitle Orchestrator

Self-hosted pipeline that auto-generates **Russian/Ukrainian** subtitles for a media library using a local GPU (Ollama for translation + Faster-Whisper for transcription), with a gatekeeper that yields the GPU the moment you start gaming.

Per title it produces labelled sidecars that sit alongside real (human) subs:

- `<video>.<ru|uk>.dub.srt` — Whisper transcript of that language's **audio track** (a dub), when present.
- `<video>.<ru|uk>.ai.srt` — LLM **translation** of an original/English subtitle, with the show's plot (from Radarr/Sonarr) and a rolling window of previous lines fed in for context/continuity.

## Components

| File | Runs on | Role |
|------|---------|------|
| `orchestrator.py` | media box (LXC with library mounted + ffmpeg) | scans the library, decides dub-vs-AI per language, drives the GPU services, writes sidecars, refreshes Jellyfin |
| `gatekeeper.py` | GPU box | reverse-proxy in front of Ollama (`:11435`→`:11434`); 503s generation when busy, unloads the model, serves `GET /gatestatus` |
| `whisper_service.py` | GPU box | wraps Purfview **Faster-Whisper-XXL** (`:11436`); accepts posted audio, returns SRT; VRAM-gated, serialized |

Audio is extracted on the media box and POSTed to the GPU box, so the NAS is never exposed to the workstation.

## Busy detection

`gatekeeper` marks the GPU busy if **non-Ollama VRAM > threshold** *or* **GPU utilization > 35%** (median of 3 samples). Utilization is the reliable game-detector — it only reads cleanly once Ollama's model has unloaded, so set `OLLAMA_KEEP_ALIVE=30s`. The orchestrator checks `/gatestatus` before each item and stops the cycle when you start playing.

## Configuration

Secrets are read from environment variables (never commit them):

- `SUBORCH_RADARR_KEY`, `SUBORCH_SONARR_KEY`, `SUBORCH_JELLYFIN_KEY`

Edit the IPs / paths / model name / thresholds at the top of each script for your environment. Gatekeeper thresholds are overridable via `GATE_THRESHOLD_MB` and `GATE_UTIL`.

## GPU box setup (Windows example)

1. Install Ollama, then `setx OLLAMA_KEEP_ALIVE 30s` and restart it.
2. Create a context-capped model so it stays fully on-GPU:
   ```
   FROM gemma3:27b
   PARAMETER num_ctx 8192
   PARAMETER temperature 0.3
   ```
   `ollama create gemma3-subs -f Modelfile`
3. Put Faster-Whisper-XXL at `whisper/Engine/faster-whisper-xxl.exe` and a model in `whisper/Model/` (not committed — see `.gitignore`).
4. Run `gatekeeper.py` and `whisper_service.py` from a stdlib-only venv via Task Scheduler at logon (use `pythonw`; both pass `CREATE_NO_WINDOW` + a hidden `STARTUPINFO` so no console flashes).
5. Firewall: allow the media box to reach the GPU box on TCP `11435` and `11436`.

## Media box setup

1. Install `python3` + `ffmpeg`.
2. Export the `SUBORCH_*` keys (e.g. via the systemd unit) and deploy `orchestrator.py`.
3. Run it on a timer (e.g. systemd `OnUnitInactiveSec=10min`) — best-effort; it no-ops when the GPU is busy/off.

## Usage

- **Automatic** — the timer drains the missing-first backlog whenever the GPU is idle.
- **On demand** — add a movie/episode to a Jellyfin playlist named `Subtitle Me`; it's generated next cycle and removed from the playlist.
- **CLI** — `orchestrator.py --force "<title substring>"`, `--limit N`, `--dry-run`.

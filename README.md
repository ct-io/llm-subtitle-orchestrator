# LLM Subtitle Orchestrator

Self-hosted pipeline that auto-generates **Russian/Ukrainian** subtitles for a media library using a local GPU (Ollama for translation + Faster-Whisper for transcription), with a gatekeeper that yields the GPU the moment you start gaming.

Per title it produces labelled sidecars that sit alongside real (human) subs:

- `<video>.<ru|uk>.dub.srt` — Whisper transcript of that language's **audio track** (a dub), when present.
- `<video>.<ru|uk>.ai.srt` — LLM **translation** of an original/English subtitle, with the show's plot (from Radarr/Sonarr) and a rolling window of previous lines fed in for context/continuity.

## Components

| File | Runs on | Role |
|------|---------|------|
| `orchestrator.py` | media box (library mounted + ffmpeg) | scans the library, decides dub-vs-AI per language, drives the GPU services, writes sidecars, refreshes Jellyfin |
| `gatekeeper.py` | GPU box | reverse-proxy in front of Ollama (`:11435`→`:11434`); 503s generation when busy, unloads the model, serves `GET /gatestatus` |
| `whisper_service.py` | GPU box | wraps Purfview **Faster-Whisper-XXL** (`:11436`); accepts posted audio, returns SRT; VRAM-gated, serialized |

Audio is extracted on the media box and POSTed to the GPU box, so the NAS is never exposed to the workstation.

```
 media box (LXC)                         GPU box (Windows + RTX GPU)
 ┌───────────────────┐   extract audio   ┌──────────────────────────────┐
 │ orchestrator.py   │ ───POST wav────▶  │ whisper_service.py :11436     │
 │  /opt/suborch     │ ◀──SRT──────────  │   └─ faster-whisper-xxl.exe    │
 │  /data/library ro │                   │ gatekeeper.py :11435 ─▶ Ollama │
 │  systemd timer    │ ───chat /v1───▶   │   :11434 (gemma3-subs, etc.)   │
 └───────────────────┘ ◀──translation──  └──────────────────────────────┘
```

---

## 1. GPU box setup (Windows example)

### Ollama + model
1. Install Ollama from <https://ollama.com/download>.
2. Make it release VRAM quickly (also lets the gatekeeper read GPU utilization cleanly):
   ```powershell
   setx OLLAMA_KEEP_ALIVE "30s"
   ```
   Quit Ollama from the tray and relaunch (or reboot) so it picks up the variable.
3. Create a **context-capped** model so it stays fully on-GPU:
   ```powershell
   "FROM gemma3:27b`nPARAMETER num_ctx 8192`nPARAMETER temperature 0.3" | Out-File -Encoding ascii gemma3-subs.Modelfile
   ollama create gemma3-subs -f gemma3-subs.Modelfile
   ```
   (Any capable multilingual model works — swap `FROM` and re-`create`. `TR_MODEL` in `orchestrator.py` must match the name.)

### Faster-Whisper
Download **Faster-Whisper-XXL** from <https://github.com/Purfview/whisper-standalone-win> and lay it out next to the scripts (these paths are hard-coded in `whisper_service.py`):
```
%USERPROFILE%\.gatekeeper\whisper\Engine\faster-whisper-xxl.exe   (+ its bundled DLLs)
%USERPROFILE%\.gatekeeper\whisper\Model\faster-whisper-large-v2\  (or your chosen model)
```
Set `MODEL` in `whisper_service.py` to the model you downloaded (default `large-v2`).

### Services (run at logon, no console window)
Put `gatekeeper.py` and `whisper_service.py` in `%USERPROFILE%\.gatekeeper\`, make a stdlib-only venv, and register both as scheduled tasks:
```powershell
python -m venv "$env:USERPROFILE\.gatekeeper\.venv"

$py  = "$env:USERPROFILE\.gatekeeper\.venv\Scripts\pythonw.exe"
$dir = "$env:USERPROFILE\.gatekeeper"
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$trg = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask -TaskName "GPU Gatekeeper"  -Force -Trigger $trg -Settings $set `
  -Action (New-ScheduledTaskAction -Execute $py -Argument "`"$dir\gatekeeper.py`"")
Register-ScheduledTask -TaskName "Whisper Service" -Force -Trigger $trg -Settings $set `
  -Action (New-ScheduledTaskAction -Execute $py -Argument "`"$dir\whisper_service.py`"")

Start-ScheduledTask -TaskName "GPU Gatekeeper"; Start-ScheduledTask -TaskName "Whisper Service"
netstat -ano | findstr "11435 11436"   # both should be LISTENING
```
Logs are written to `%USERPROFILE%\.gatekeeper\gatekeeper.log` / `whisper.log` (pythonw has no console).

### Firewall (on the GPU box)
Allow only the media box to reach the two ports:
```powershell
New-NetFirewallRule -DisplayName "GPU Gatekeeper" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11435 -RemoteAddress <MEDIA_BOX_IP>
New-NetFirewallRule -DisplayName "Whisper"        -Direction Inbound -Action Allow -Protocol TCP -LocalPort 11436 -RemoteAddress <MEDIA_BOX_IP>
```
If the boxes are on different VLANs/subnets, also add the matching allow rule on your router/firewall (media box → GPU box, TCP 11435 & 11436).

---

## 2. Media box setup (Debian/Ubuntu example)

The box must have the media library mounted at the paths in `ROOTS` (default `/data/library/movies`, `/data/library/tv`) with **write** access, so sidecars can be created next to the videos. The script `chmod 0664`s them so the media server can read them.

```bash
apt-get update && apt-get install -y python3 ffmpeg
mkdir -p /opt/suborch/cache
# copy orchestrator.py to /opt/suborch/orchestrator.py
```

### Get the API keys
- **Radarr**: Settings → General → Security → **API Key**
- **Sonarr**: Settings → General → Security → **API Key**
- **Jellyfin**: Dashboard → Administration → **API Keys** → **+** (used for the `Subtitle Me` playlist trigger and library refresh)

Store them in an env file (kept out of git):
```bash
cat > /opt/suborch/suborch.env <<'EOF'
SUBORCH_RADARR_KEY=your_radarr_key
SUBORCH_SONARR_KEY=your_sonarr_key
SUBORCH_JELLYFIN_KEY=your_jellyfin_key
EOF
chmod 600 /opt/suborch/suborch.env
```

Also edit the constants at the top of `orchestrator.py` for your setup: `ROOTS`, `CACHE_DIR`, the `CROCELL` GPU-box IP, `RADARR`/`SONARR`/`JELLYFIN` base URLs, `TR_MODEL`, `TARGETS`.

### systemd service + timer
`/etc/systemd/system/suborch.service`:
```ini
[Unit]
Description=Subtitle orchestrator (dub/AI variants)
After=network-online.target

[Service]
Type=simple
EnvironmentFile=/opt/suborch/suborch.env
ExecStart=/usr/bin/python3 /opt/suborch/orchestrator.py
```
`/etc/systemd/system/suborch.timer`:
```ini
[Unit]
Description=Run subtitle orchestrator periodically

[Timer]
OnBootSec=10min
OnUnitInactiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
```
Enable and watch:
```bash
systemctl daemon-reload
systemctl enable --now suborch.timer
journalctl -u suborch.service -f
```
`OnUnitInactiveSec=10min` re-checks the GPU 10 minutes after each run finishes. A run no-ops in ~1s when the GPU is busy/off, and drains the missing-first backlog when it's idle.

---

## 3. Verify

From the media box:
```bash
curl -s http://<GPU_BOX_IP>:11435/gatestatus     # {"util":..,"other_apps_mb":..,"busy":..}
python3 /opt/suborch/orchestrator.py --dry-run    # lists titles it would process
python3 /opt/suborch/orchestrator.py --limit 1    # process one title end-to-end
```
Launch a game and re-check `/gatestatus` — `util` should jump and `busy` flip to `true`.

## 4. Usage

- **Automatic** — the timer drains the missing-first backlog whenever the GPU is idle.
- **On demand** — create an (initially empty) Jellyfin playlist named exactly `Subtitle Me`; add any movie/episode to it and it's generated next cycle (all variants, even if it already has subs), then removed from the playlist.
- **CLI** — `orchestrator.py --force "<title substring>"` (best variant per language, now), `--limit N`, `--dry-run`.

## Configuration reference

Top-of-file constants:

- `orchestrator.py`: `ROOTS`, `TARGETS`, `CROCELL` (GPU-box IP), `TR_MODEL`, `CACHE_DIR`, `BATCH` (lines per translation request), `SLEEP` (seconds between items). Secrets via `SUBORCH_RADARR_KEY` / `SUBORCH_SONARR_KEY` / `SUBORCH_JELLYFIN_KEY`.
- `gatekeeper.py`: busy = non-Ollama VRAM `> GATE_THRESHOLD_MB` (default 7000) **or** GPU utilization `> GATE_UTIL` (default 35). Both overridable via env on the task.
- `whisper_service.py`: `MODEL`, `LISTEN_PORT` (11436).

## Notes

- Only **text** subtitles (subrip/ass/mov_text…) are used as AI-translation sources; bitmap subs (PGS/VobSub) are skipped (they'd need OCR).
- Both Windows services pass `CREATE_NO_WINDOW` + a hidden `STARTUPINFO` to every subprocess so no console flashes during work — restart the scheduled task after editing either script.
- The `whisper/` engine+models and the `.venv` are intentionally **not** committed (see `.gitignore`).

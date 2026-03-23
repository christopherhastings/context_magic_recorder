# Recorder — Start Here

## Folder structure

```
recorder/
├── recorder_daemon.py      ← main process
├── zoom_detector.py        ← Zoom call detection
├── processor.py            ← Whisper + diarization pipeline
├── zoom_cloud.py           ← Zoom Cloud API enrichment (optional)
├── archiver.py             ← WAV → Opus after processing
├── audio_router.py         ← system audio routing for Safari Meet
├── permissions.py          ← macOS permission checks at startup
├── api_server.py           ← transcript viewer API (localhost:8766)
├── menubar.py              ← menu bar status app
├── transcript_viewer.jsx   ← React transcript viewer
├── requirements.txt
├── .env.example
├── com.recorder.plist      ← launchd auto-start
├── chrome_extension/       ← load in Chrome
│   ├── manifest.json
│   ├── background.js
│   ├── content.js
│   ├── offscreen.html
│   ├── offscreen.js
│   └── popup.html
└── safari_extension/       ← package via Xcode (pending Apple ID)
    ├── manifest.json
    ├── background.js
    ├── content.js
    └── popup.html
```

---

## One-time setup

### 1. System dependencies

```bash
brew install ffmpeg blackhole-2ch switchaudio-osx
```

### 2. Audio MIDI Setup — Multi-Output Device

Open **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup"):
1. Click **+** → **Create Multi-Output Device**
2. Tick: **BlackHole 2ch** + **MacBook Pro Speakers** + any headphones you own
3. Double-click the device name → rename to **Recorder Output**

Set Zoom's speaker to BlackHole:
- Zoom → Settings → Audio → Speaker → **BlackHole 2ch**

System output stays on your normal device. Safari Meet switches
temporarily to the Multi-Output Device during a call, then switches back.

### 3. Python environment

```bash
cd ~/recorder
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 4. Configure

```bash
cp .env.example .env
# Edit .env — minimum required:
#   BLACKHOLE_DEVICE=BlackHole 2ch
#   OUTPUT_DIR=/Users/christopher/Recordings
#   HF_TOKEN=hf_...   (get from huggingface.co/settings/tokens)
```

To get your HuggingFace token:
1. huggingface.co → sign in → Settings → Access Tokens → New token
2. Accept pyannote terms at: huggingface.co/pyannote/speaker-diarization-3.1
3. Paste token into `.env` as `HF_TOKEN=hf_...`

**Daemon logging** (optional `.env`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail (all loggers). |
| `LOG_FILE` | `/tmp/recorder_daemon.log` | Main log path; parent dirs are created if needed. |
| `LOG_MAX_BYTES` | `0` | If &gt; `0`, rotate logs at this size (bytes), e.g. `5242880` (5 MiB). |
| `LOG_BACKUP_COUNT` | `5` | Kept rotated files when `LOG_MAX_BYTES` is set. |
| `ZOOM_DETECT_DEBUG` | off | `1` / `true` → DEBUG logs every Zoom poll + window titles (noisy). |

Follow with: `tail -f /tmp/recorder_daemon.log` (or your `LOG_FILE`).

### 5. macOS permissions

The daemon will check these on first launch and open the right
System Settings panes automatically. But to do it manually:

- **Accessibility**: System Settings → Privacy & Security → Accessibility → add Terminal
- **Notifications**: System Settings → Notifications → Terminal → Allow

### 6. Chrome extension

1. `chrome://extensions` → enable **Developer mode** (top right)
2. **Load unpacked** → select `chrome_extension/` folder
3. Grant permission for `meet.google.com` when prompted

### 7. Safari extension

Pending your Apple Developer account approval. Instructions in `SETUP.md`.

---

## Starting up

Three processes. Open three terminal tabs:

```bash
# Tab 1 — main daemon (Zoom detection + Meet WebSocket server)
source venv/bin/activate
python recorder_daemon.py

# Tab 2 — transcript viewer API
source venv/bin/activate
python api_server.py

# Tab 3 — menu bar app
source venv/bin/activate
python menubar.py
```

Transcript viewer: **http://localhost:8766**

---

## Auto-start on login (optional)

```bash
# Edit the plist — sets correct username and paths
sed -i '' "s/YOUR_USERNAME/$USER/g" com.recorder.plist

# Install (do this once per process — clone for api_server and menubar)
cp com.recorder.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.recorder.plist
```

---

## Menu bar states

| Icon | Meaning |
|------|---------|
| `⬤` | Ready — no active recording |
| `⏺ Meeting name` | Recording in progress |
| `◌` | Processing (transcription running) |
| `⚠` | Error — check notification |

---

## Output per meeting

```
~/Recordings/
├── 2026-02-20_09-02-15_zoom_Weekly_Sync.opus    ← archived audio (~14MB/hr)
├── 2026-02-20_09-02-15_zoom_Weekly_Sync.json    ← full transcript + metadata
└── 2026-02-20_09-02-15_zoom_Weekly_Sync.md      ← readable transcript
```

Filename encodes: date, time, source (zoom/meet-chrome/meet-safari), topic.

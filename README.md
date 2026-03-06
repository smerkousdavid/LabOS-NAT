# LabOS NAT Server

Agent server for laboratory protocol assistance. Uses the OpenAI Agents SDK with tool-calling to run multi-turn conversations, manage lab protocols, and monitor experiments via STELLA VLM.

Connects to **labos-models** for LLM/VLM inference and serves **labos-runtime** desktop clients over WebSocket.

## Architecture

```
labos-runtime (Desktop)     labos-nat (this repo)         labos-models (GPU)
┌──────────────────┐    ┌─────────────────────┐    ┌──────────────────┐
│  Voice Bridge    │───►│  WebSocket :8002     │    │  LLM  :8001      │
│  (STT, wake word)│    │  ┌─────────────┐    │───►│  VLM  :8500      │
│                  │◄───│  │ Agent       │    │    └──────────────────┘
│  Dashboard :5001 │    │  │ (20 tools)  │    │
│  gRPC :5050      │    │  └─────────────┘    │
└──────────────────┘    │  Protocols, VSOP    │
                        └─────────────────────┘
```

## Quick Start

```bash
# 1. Copy and edit config
cp config.yaml.example config.yaml
cp .env.secrets.example .env.secrets
# Set model endpoints (labos-models IP), add SERPAPI_KEY if desired

# 2. Generate runtime config
python configure.py

# 3. Start the server
docker compose up -d

# Server is at ws://localhost:8002/ws
```

## Configuration

Edit `config.yaml`:

- **`nat`** -- Server host/port, session limits, VSOP provider, tool toggles
- **`models`** -- LLM and VLM endpoints (point to labos-models)
- **`secrets_file`** -- Path to `.env.secrets` for API keys

## WebSocket Protocol

Connect at `ws://<host>:8002/ws?session_id=<id>`

### Inbound (Runtime -> NAT)

| Type | Fields | Purpose |
|---|---|---|
| `user_message` | `text` | Transcribed speech |
| `frame_response` | `request_id`, `frames` | Camera frame captures (base64 JPEG) |
| `stream_info` | `camera_index`, `rtsp_base`, `paths` | Camera metadata (sent on connect) |
| `ping` | -- | Keepalive |

### Outbound (NAT -> Runtime)

| Type | Fields | Purpose |
|---|---|---|
| `agent_response` | `text`, `tts` | Agent reply (with TTS flag) |
| `notification` | `text`, `tts` | Async notification |
| `display_update` | `message_type`, `payload` | Rich panel for glasses display |
| `request_frames` | `request_id`, `count`, `interval_ms` | Request camera snapshots |
| `tts_only` | `text`, `priority` | Speak without display |
| `wake_timeout` | `seconds` | Update wake word timeout |

## Tool Catalog (20 tools)

Protocol management, STELLA VLM monitoring, web search, code execution, datetime, display/TTS, and history summarization. Toggle tools in `config.yaml` under `nat.tools`.

## HTTP Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Health check |
| GET/PUT | `/tools/catalog` | List or toggle tools |
| POST | `/clear_memory` | Clear session history |

## API Keys

| Key | Purpose | Required |
|---|---|---|
| `SERPAPI_KEY` | Web search tool | Optional (falls back to DuckDuckGo) |
| `DASHSCOPE_API_KEY` | Qwen TTS | Only if using Qwen provider |
| `GOOGLE_API_KEY` | Gemini Live API (when `gemini_live.enabled`) | Optional |

## LabOS Live Session Integration

Optional WebSocket bridge that streams protocol events, chat, and VLM monitoring data to a LabOS web frontend for real-time display and recording. Enabled when an XR device scans a QR code from the LabOS web UI.

### Config

```yaml
# In config.yaml under nat:
labos_live:
  enabled: false          # master switch
  initial_qr_code: false  # show QR scanning prompt on XR startup
```

### How It Works

1. The LabOS web frontend shows a QR code containing a session payload (`session_id`, `ws_endpoint`, `token`, `publish_rtsp`)
2. XR glasses scan the QR code; the runtime sends the payload to NAT
3. NAT opens a second WebSocket to the LabOS server at `ws_endpoint`
4. All protocol events are fan-out to both the XR glasses (existing path) and the LabOS server (new path)
5. The LabOS server records everything and pushes to the web frontend in real-time

### Events Sent to LabOS (NAT -> LabOS)

| Type | When | Fields |
|------|------|--------|
| `chat` | User speaks or agent replies | `source` (user/assistant), `message` |
| `monitoring` | VLM observation update (~5s) | `message` |
| `protocol_start` | `start_protocol` called | `name`, `steps` [{step, short, long}] |
| `protocol_change_step` | Step navigation | `name`, `previous_step`, `step` |
| `protocol_error` | Error detected | `name`, `error` |
| `protocol_data` | Data logged | `name`, `data` |
| `protocol_stop` | Protocol ended | -- |
| `stream_started` | RTSP relay active | -- |
| `end_stream` | Session ending | -- |

### Events Received from LabOS (LabOS -> NAT)

| Type | Purpose |
|------|---------|
| `start_protocol_by_text` | Web user pushes a protocol to the AR device |
| `pong` | Keepalive response |
| `error` | Malformed message notification |

### QR Code Payload Format

```json
{
  "type": "labos_live",
  "api_base": "https://labos.example.com",
  "session_id": "uuid-xxx",
  "token": "abc123",
  "ws_endpoint": "wss://labos.example.com/ws/vlm/uuid-xxx",
  "publish_rtsp": "rtsp://mediamtx-host:8554/live/uuid-xxx"
}
```

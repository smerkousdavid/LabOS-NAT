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

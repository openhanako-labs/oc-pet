# OC Desktop Pet

![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)

## ⚠️ Live2D Model Copyright Notice

This project does **not** distribute any Live2D model files (`characters/*/live2d/` is excluded via `.gitignore`).

- The built-in character 「月薪喵」 uses **sprite frames** (`frames/`), which can be distributed with the project.
- The Live2D renderer code is fully included, but **you must provide your own model** — use a model with a redistribution license (e.g. [Live2D official samples](https://www.live2d.com/en/learn/sample/)), or run `python tools/fetch_free_live2d_sample.py` to download the official Haru sample.
- Do NOT put models without redistribution rights (e.g. game-ripped models) into the repository.

An AI desktop companion built on PySide6, deeply integrated with the Hanako ecosystem. Supports running multiple desktop pets in parallel, where each Hanako agent can independently own its own pet window.

<details>
<summary>Feature List</summary>

### Conversation System
- 💬 **Text Chat** -- Reuses Hanako identity / memory / model configuration, supports tool calling
- 🗣️ **TTS Voice Output** -- Three selectable engines:
  - CosyVoice2 local cloning (zero-shot cloning, requires GPU)
  - MIMO TTS (Xiaomi MiMo V2.5, selectable voice)
  - OpenAI-compatible API
- 🎤 **ASR Voice Input** -- Three selectable engines:
  - Whisper local (offline recognition)
  - MIMO ASR (Xiaomi MiMo V2.5)
  - OpenAI-compatible API
- 🔌 **Plugin Tool Invocation** -- Automatically scans Hanako plugins and executes plugin tools via LLM tool calling

### Perception System
- ⏰ **Time Awareness** -- Distinguishes morning / noon / afternoon / evening / late night / early morning, influencing conversation style
- 😊 **Emotion State Machine** -- Five emotions: happy / sad / thinking / surprised / neutral, with automatic decay
- 📸 **Screen Awareness** -- Periodic screenshots + vision model analysis, injected into conversation context
- 🎭 **On-screen Emotion Detection** -- Infers user emotion from screen content (e.g. "watching video" → happy)
- 🪟 **Foreground Window Monitoring** -- Detects the app the user is currently using, for window interaction and proactive conversation triggers
- 📱 **Phone Activity Awareness** -- MacroDroid reports foreground app switches, automatically categorized (entertainment / communication / music / shopping / reading / work / gaming) and injected into context
- 🔌 **Palm Window Integration** -- Retrieves phone screenshots, lifestyle status (battery / network), and remote control (open app / notifications / alarms) via the linjian-peek service

### Narrative Engine
- 📝 **Micro-event Generation** -- Automatically generates small events when idle (observation / care / joke / question / greeting)
- 📦 **Local Template Fallback** -- Uses preset templates when the LLM is unavailable (no disconnection needed, only the API needs to be unreachable)
- 🔄 **Context Cache + Cooldown Control** -- Avoids duplicate content, 600-second cooldown (configurable)

### Interaction Features
- 🖱️ **Mouse Interaction** -- Gaze following + proximity reaction + hover + chase + fright
- 🖐️ **Dragging** -- Left-click drag the pet, bounces after release
- 📌 **Edge Snapping** -- Drag to the screen edge to sit down
- 🪟 **Window Interaction** -- Detects the foreground window; the pet automatically walks over (cooldown configurable)
- 💬 **Right-click Menu** -- Click-through / Settings / Plugins / Exit
- ⌨️ **Chat Box** -- Left-click to toggle chat input

### Proactive Conversation
- 🤖 **Rule Engine** -- Idle duration + foreground window category → automatic initiation
- 📊 **Screen Content Trigger** -- Proactively initiates based on screen analysis results (detects keywords like video / game / code)
- ⏱️ **Cooldown Control** -- 5-minute cooldown for screen content triggers, avoiding frequent interruptions

### Hanako Integration
- 🔗 **Status Monitoring** -- Reads Hanako status in real time (TODO / notifications / conversation replies)
- 💬 **Conversation Sync** -- When Hanako has a new reply, the pet shows a bubble + plays TTS
- 📋 **Working Status** -- When Hanako has a TODO, the pet shows a "working" status
- 🔔 **Notification Forwarding** -- Hanako notifications are displayed as pet message bubbles
- 🌐 **Multi-pet Collaboration** -- Multiple pets can "chat" / react / care / gift each other
- 📁 **Memory Reading** -- Reads Hanako's pinned memory and recent conversation history

### Phone Awareness (Dual Channel)

The pet perceives phone status through two independent channels, uniformly injected into the LLM context:

**Channel 1: MacroDroid Direct Connection (Persistent Awareness)**
- 📱 **Foreground App Reporting** -- MacroDroid rules detect app switches and HTTP POST to the pet's local receiver
- 🏷️ **Auto Categorization** -- 7 app categories (entertainment / communication / music / shopping / reading / work / gaming) + emotion mapping
- 📊 **Activity Summary** -- "Used Xiaohongshu (3 times), WeChat (2 times) in the last 1 hour"
- ⏱️ **Idle Detection** -- Minutes since the last phone activity
- 🔒 **Privacy First** -- Data never leaves the local machine, standard-library HTTP server, zero external dependencies

**Channel 2: Palm Window Integration (On-demand Enhancement)**
- 📸 **Phone Screenshot** -- Requests and returns a phone screenshot via the linjian-peek service
- 🔋 **Lifestyle Status** -- Battery, charging, network, current app, screen time, unlock count
- 🎮 **Remote Control** -- Open app, return to home, send notification, set alarm
- 🔌 **MCP Tools** -- Registered via the Hanako plugin system, triggered by LLM tool calling

**Data Flow:**
```
MacroDroid → HTTP POST → PhoneActivityReceiver → PhoneActivityPerception ─┐
                                                                           ├→ PerceptionController.build_context()
linjian-peek → MCP Plugin → Hanako tool calling ──────────────────────────┘
```

### Memory System
- 💾 **Memory Snapshot** -- Export / import agent memory, supports overwrite / smart / skip_existing merging
- 📏 **Dynamic Memory Budget** -- Automatically calculated as 1% of the model context, or manually specified as a character count
- 📌 **Pinned Memory** -- Reads pinned-memory.json

### Multi-pet
- 🏠 **Multi-window Parallel** -- Each Hanako agent independently runs one pet
- 🔍 **Agent Discovery** -- Automatically scans `~/.hanako/agents/`
- 🎨 **Character Package Management** -- Custom sprites + built-in fallback

### MCP Tools (the pet can be driven *by* AI)

The pet ships a built-in MCP server (default `http://127.0.0.1:8979/mcp`)
exposing its expression / motion / perception abilities to MCP clients such as
Hanako — **the pet is not just a shell being driven; AI can operate it too**.
**21 tools** total:

| Group | Tools | Purpose |
|---|---|---|
| State | `pet_state` / `pet_capabilities` | Read current emotion / motion / capability snapshot |
| Expression | `pet_set_emotion` / `pet_play_anim` / `pet_expression` / `pet_say` / `pet_celebrate` / `pet_reset_idle` | Set emotion, play motion, set expression, speak, celebrate, reset idle |
| Computer | `pet_computer_*` (8) | Enumerate windows / element tree / launch / click / type / key (read-only unless actions are explicitly enabled) |
| Hanako | `pet_hana_*` (5) | Read Hanako status / sessions / agents / apps |

> **Toggle**: `config.json` → `mcp_server.enabled`. Default port 8979,
> bound to `127.0.0.1` only (never exposed externally).
> **Computer actions are off by default**: with `computer_use.allow_actions=false`
> the pet only reads window info and will not click or type.

### Notifications
- 📱 **ntfy Notification** -- Push notifications to phone (requires the ntfy app installed)

</details>

## Environment Requirements

- **Python**: 3.10+
- **OS**: Windows 10/11
- **Hanako**: Installed and configured (the pet reads configuration and character data under `~/.hanako/`)

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Ensure Hanako is Installed

The pet reads from Hanako:
- `~/.hanako/agents/<agent>/` - identity, consciousness, memory, model configuration
- `~/.hanako/provider-catalog.json` - API address, keys, model list

No separate API configuration is needed; it automatically reuses Hanako's.

### 3. Launch

```bash
python main.py
```

Or double-click `start_pet.bat`.

On first run, **Yuexin Miao (yuexinmiao)** is automatically added as the default pet.

## Configuration

### config.json

```json
{
  "behavior": "normal",           // Behavior mode: quiet/normal/active/cling
  "window_interaction": {
    "enabled": true,              // Whether to enable window interaction
    "cooldown_seconds": 30        // Window interaction cooldown (seconds)
  },
  "memory": {
    "budget_chars": 0,            // Memory budget character count (0 = auto)
    "budget_percent": 1.0         // Auto mode: percentage of model context
  },
  "tts": {
    "enabled": true,
    "provider": "mimo",           // TTS engine: cosyvoice/mimo/api
    "volume": 0.8
  },
  "asr": {
    "provider": "whisper_local"   // ASR engine: whisper_local/mimo/api
  },
  "proactive": {
    "enabled": true,
    "cooldown_minutes": 10        // Proactive conversation cooldown
  },
  "screen": {
    "enabled": true,
    "interval": 120,              // Screenshot interval (seconds)
    "blur": true                  // Blur screenshots (privacy protection)
  }
}
```

### .env file

```env
# LLM (optional, Hanako configuration takes priority)
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat

# TTS (optional)
TTS_PROVIDER=mimo
TTS_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
TTS_API_KEY=sk-xxx

# ASR (optional)
ASR_PROVIDER=whisper_local

# Vision model (optional, used for screen awareness)
VISION_BASE_URL=https://api.siliconflow.cn
VISION_API_KEY=sk-xxx
VISION_MODEL=Qwen/Qwen2.5-VL-7B-Instruct

# ntfy notification (optional)
NTFY_TOPIC=your-topic-name

# Phone activity awareness - MacroDroid direct connection (optional)
PHONE_RECEIVER_PORT=8077
PHONE_AUTH_TOKEN=your-secret-token

# Palm window - linjian-peek integration (optional)
LINJIAN_URL=https://xxx.onrender.com
LINJIAN_TOKEN=your-linjian-token
```

### MacroDroid Configuration (Phone Activity Reporting)

1. Install [MacroDroid](https://play.google.com/store/apps/details?id=com.arlosoft.macrodroid) (Android)
2. Create a new macro: Trigger = "Application Launch/Switch" → Action = "HTTP Request"
3. HTTP request configuration:
   - Method: `POST`
   - URL: `http://<PC-IP>:8077/phone/activity`
   - Header: `X-Auth-Token: <your-token>`
   - Body: `{"app": "{app_name}", "event": "switch"}`
4. Save and enable the macro

> 💡 If the pet and phone are on the same LAN, use the PC's internal IP. For external access, consider using ngrok or frp for intranet penetration.

## Testing Guide

| Feature | Test Method | Expected Result |
|---------|-------------|-----------------|
| Drag | Left-click drag the pet | Pet follows mouse movement |
| Edge Snapping | Drag to screen edge | Pet sits down |
| Mouse Following | Mouse approaches the pet | Pet gaze follows |
| Window Interaction | Switch foreground app | Pet walks over |
| Screen Awareness | Wait 2 minutes | Log shows `Screen analysis: ...` |
| Narrative Engine | Wait 10 minutes | Pet talks to itself |
| Chat | Left-click the pet | Chat box pops up |
| Settings | Right-click menu → Settings | Settings panel opens |
| Phone Awareness | MacroDroid POST to localhost:8077 | Log shows `Phone activity: app=小红书 event=switch` |
| Palm Window Status | Call `phone_status` during Hanako conversation | Returns service online status |

## Architecture

```
PetManager (Multi-pet Manager)
  ├─ PetWindow[yuexinmiao] ── ConversationEngine ── HanakoPetAdapter (LLM)
  │    ├─ SpriteRenderer (Sprite Rendering)
  │    ├─ MouseTracker (Mouse Interaction)
  │    ├─ PerceptionController (Perception)
  │    │    ├─ ScreenWatcher (Screen Awareness)
  │    │    ├─ PhoneActivityPerception (Phone Activity)
  │    │    ├─ PhoneActivityReceiver (MacroDroid HTTP)
  │    │    ├─ ProactiveScheduler (Proactive Conversation)
  │    │    └─ EmotionStateMachine (Emotion)
  │    ├─ NarrativeEngine (Narrative Engine)
  │    ├─ WindowInteraction (Window Interaction)
  │    ├─ Bubble (Dialog Bubble)
  │    └─ PluginPanel (Plugin Panel)
  └─ SettingsDialog (Settings)
       ├─ LLM/TTS/ASR Provider Selection
       ├─ Agent Management
       └─ Memory/Behavior/Schedule Configuration
```

## Directory Structure

```
oc-pet/
├── main.py                 # Entry point
├── pet_manager.py          # Multi-pet management
├── pet.py                  # Single pet window (main logic)
├── config.py               # Configuration management
├── env_config.py           # .env configuration
├── core/                   # Core modules
│   ├── conversation_engine.py  # Conversation engine
│   ├── harness_adapter.py      # LLM adapter
│   ├── perception.py           # Perception system (time/emotion/screen/phone/proactive)
│   ├── phone_activity.py       # Phone activity data management + perception layer
│   ├── phone_receiver.py       # MacroDroid HTTP receiver
│   ├── narrative_engine.py     # Narrative engine
│   ├── window_interaction.py   # Window interaction
│   ├── hanako_bridge.py        # Hanako integration (status reading)
│   ├── hanako_monitor.py       # Hanako monitoring (TODO/notifications/replies)
│   ├── multi_pet_bridge.py     # Multi-pet collaboration (event communication)
│   ├── tool_registry.py        # Tool registry
│   ├── tool_executor.py        # Tool executor
│   ├── hanako_context.py       # Context building
│   └── memory_snapshot.py      # Memory snapshot
├── ui/                     # UI modules
│   ├── settings_dialog.py      # Settings panel
│   ├── plugin_panel.py         # Plugin panel
│   └── bubble.py               # Dialog bubble
├── avatar/                 # Sprite rendering
│   └── sprite_renderer.py
├── motion/                 # Motion system
│   ├── physics.py              # Physics engine
│   ├── behavior.py             # Behavior state machine
│   └── foreground_watcher.py   # Foreground window monitoring
├── tts_provider/           # TTS engines
├── asr_provider/           # ASR engines
├── characters/             # Built-in characters
│   └── yuexinmiao/             # Yuexin Miao (default)
└── requirements.txt        # Dependency list
```

## FAQ

### Q: The pet doesn't speak?
A: Check the LLM API configuration. The pet automatically uses Hanako's configuration; if Hanako is not configured, you need to specify it in `.env`.

### Q: TTS doesn't work?
A: TTS is an optional feature and does not affect text chat. Switch the TTS engine in the settings panel.

### Q: Screen awareness doesn't trigger proactive conversation?
A: In the current version, screen awareness only triggers emotion, not proactive conversation. Proactive conversation is triggered by ProactiveScheduler based on idle time and foreground window.

### Q: How to add more pets?
A: Add them in the "Character Package" section of the settings panel, or create a new agent directory under `~/.hanako/agents/`.

### Q: How to use ntfy notifications?
A: 1) Install the ntfy app on your phone (Android/iOS); 2) Subscribe to a topic; 3) Configure `NTFY_TOPIC=your-topic` in `.env`.

## License

This project uses a **dual license**:

- **Open Source License**: [GNU AGPL v3](https://www.gnu.org/licenses/agpl-3.0.html) -- free for open source, but modifications must remain open source
- **Commercial License**: Closed-source usage requires purchasing a commercial license, see [COMMERCIAL-LICENSE.md](./COMMERCIAL-LICENSE.md)

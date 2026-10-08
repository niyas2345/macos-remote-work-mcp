# macOS Remote Work & Accessibility MCP Server

A local **MCP (Model Context Protocol) server for macOS Monterey+** that exposes
10 remote-work and accessibility controls to **ChatGPT** and **Grok** via
function calling, plus a small HTTP bridge those LLMs can call. Includes the
[apple_cursor](https://github.com/ful1e5/apple_cursor) theme preset.

- **10 tools**: system status, cursor themes, accessibility profiles, pointer
  size, display contrast, VoiceOver, backup/restore
- **Local-only**: binds `127.0.0.1` only, user-level settings, no sudo, no cloud
- **One-command setup**: `./setup.sh`
- **Logged**: every tool call lands in `~/.macos-remote-work/logs.json`

---

## Quick start

```bash
git clone <this-repo> macos-remote-work-mcp
cd macos-remote-work-mcp
./setup.sh
```

`setup.sh` will:

1. Verify macOS 12+ and Python 3.9+
2. Create a `.venv` and install `fastmcp`, `fastapi`, `uvicorn`, `requests`
3. Initialize `cursor_themes.json` & `macos_profiles.json` (if missing)
4. Start the MCP server (`:8000`) and Grok adapter (`:8010`) in the background
5. Verify connectivity and print your configuration URLs

---

## Architecture

```
 ChatGPT Custom GPT                        Grok
 (function schemas)                  (function calling)
        |                                  |
        | chatgpt_tools.json               | examples/grok_tool_config.json
        |                                  |
        |            POST /tool {"tool": ..., "arguments": {...}}
        |                                  |
        v                                  v
 +------------------------------------------------------------------+
 | grok_adapter.py - FastAPI bridge (127.0.0.1:8010)                |
 |   GET  /health   GET /tools   POST /tool                        |
 +-------------------------------+----------------------------------+
                                 | MCP (streamable HTTP, /mcp)
                                 v
 +------------------------------------------------------------------+
 | mcp_server.py - FastMCP server (127.0.0.1:8000)                  |
 |   get_system_status        list_cursor_themes                    |
 |   apply_cursor_theme       list_accessibility_profiles           |
 |   create_accessibility_profile                                  |
 |   apply_remote_work_profile  set_pointer_size                    |
 |   set_display_contrast      enable_voiceover                     |
 |   backup_macos_settings                                          |
 +------+--------------------------+------------------------+------+
        |                          |                        |
        v                          v                        v
 defaults write            osascript / open          JSON state files
 (user-level prefs)        (VoiceOver, Cmd+F5)       cursor_themes.json
        |                          |                macos_profiles.json
        v                          v                        |
 +------------------------------------------------------------------+
 | macOS (Monterey 12+) - user session, no sudo                     |
 | ~/.macos-remote-work/logs.json  (audit log, last 500 calls)      |
 | ~/.macos-remote-work/backups/   (settings backups)               |
 +------------------------------------------------------------------+
```

---

## The 10 tools

| Tool | Arguments | What it does |
| --- | --- | --- |
| `get_system_status` | - | macOS version, uptime, live settings, active profile/theme |
| `list_cursor_themes` | - | Lists the 4 cursor themes + active theme |
| `apply_cursor_theme` | `theme_name` | Applies a theme's pointer size + contrast |
| `list_accessibility_profiles` | - | Lists profiles + active profile + remote-work mode |
| `create_accessibility_profile` | `profile_name` (+ optional settings) | Creates a custom profile; omitted settings snapshot current state |
| `apply_remote_work_profile` | `profile_name` | Applies pointer/contrast/keyboard/VoiceOver/remote-work state |
| `set_pointer_size` | `size` (0.5-3.0) | Sets pointer size via `defaults write` |
| `set_display_contrast` | `level` (`low`/`normal`/`high`/`max`) | Increase Contrast / Reduce Transparency / Differentiate Without Color |
| `enable_voiceover` | - | Enables VoiceOver (Cmd+F5); idempotent, never toggles off |
| `backup_macos_settings` | `restore_from?` | Backs up settings to `~/.macos-remote-work/backups/`; restores when given a path |

Every call returns JSON:

```json
{"success": true, "tool": "set_pointer_size", "message": "Pointer size set to 2.0.", "timestamp": "...", "...": "metadata"}
```

### Built-in profiles (`macos_profiles.json`)

| Profile | Pointer | Contrast | VoiceOver | Keyboard nav | Remote-work mode |
| --- | --- | --- | --- | --- | --- |
| Remote Work | 1.5 | high | off | on | **on** |
| Accessibility Power User | 2.0 | max | **on** | on | off |
| Developer Focus | 1.3 | high | off | on | off |
| High Contrast | 2.0 | max | off | on | off |

Custom profiles can be created by the LLM (or you) at runtime.

### Cursor themes (`cursor_themes.json`)

| Theme | Pointer | Contrast | Source |
| --- | --- | --- | --- |
| `apple_cursor` | 1.5 | high | [ful1e5/apple_cursor](https://github.com/ful1e5/apple_cursor) |
| `high_contrast` | 1.5 | max | builtin |
| `large_pointer` | 3.0 | normal | builtin |
| `remote_work_clear` | 1.5 | high | builtin |

---

## Running manually

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# terminal 1 - MCP server on :8000
.venv/bin/python mcp_server.py

# terminal 2 - Grok/ChatGPT bridge on :8010
.venv/bin/python grok_adapter.py
```

Stop services started by `setup.sh`:

```bash
kill "$(cat .run/mcp.pid)" "$(cat .run/adapter.pid)"
```

---

## Usage with ChatGPT

1. Copy [`examples/chatgpt_custom_gpt_instructions.md`](examples/chatgpt_custom_gpt_instructions.md)
   into your Custom GPT's **Instructions** field.
2. Use [`chatgpt_tools.json`](chatgpt_tools.json) as the function schema in your
   OpenAI-compatible client (it defines all 10 functions).
3. Route calls to the bridge:

```bash
curl -s http://127.0.0.1:8010/tool \
  -H 'Content-Type: application/json' \
  -d '{"tool": "list_accessibility_profiles", "arguments": {}}'
```

> ChatGPT Actions execute from OpenAI's cloud and **cannot reach
> `127.0.0.1`**. Use the schema with a local function-calling client, or expose
> the bridge through a tunnel you control (see Security notes first).

## Usage with Grok

1. Register the tools from [`examples/grok_tool_config.json`](examples/grok_tool_config.json)
   (OpenAI/xAI function-calling format).
2. For each tool call, send:

```json
POST http://127.0.0.1:8010/tool
{"tool": "apply_cursor_theme", "arguments": {"theme_name": "apple_cursor"}}
```

3. Inspect what's available: `GET http://127.0.0.1:8010/tools`

Example session:

```bash
curl -s http://127.0.0.1:8010/tools | python3 -m json.tool
curl -s http://127.0.0.1:8010/tool -d '{"tool":"set_pointer_size","arguments":{"size":2.0}}'
curl -s http://127.0.0.1:8010/tool -d '{"tool":"get_system_status","arguments":{}}'
```

---

## apple_cursor integration

[`cursor_themes.json`](cursor_themes.json) ships an `apple_cursor` preset that
references the open-source cursor project
**[ful1e5/apple_cursor](https://github.com/ful1e5/apple_cursor)** (crisp,
colorful, pixel-perfect macOS-style cursors):

- `source` field points at the repo so LLMs can cite it.
- Applying the preset configures the matching **accessibility look**
  (pointer size `1.5`, contrast `high`) so the cursor stays visible during
  screen shares while you install the apple_cursor images themselves from the
  repo's releases (macOS has no built-in third-party cursor installer - follow
  the upstream repo's instructions for the image swap).

Other presets (`high_contrast`, `large_pointer`, `remote_work_clear`) are
fully builtin.

---

## Backup & restore

```bash
# via a tool call (also available in the LLM chat)
curl -s http://127.0.0.1:8010/tool -d '{"tool":"backup_macos_settings","arguments":{}}'
# -> {"backup_file": "~/.macos-remote-work/backups/mac_settings_YYYYMMDD_HHMMSS.plist", ...}

# restore later (path must be inside ~/.macos-remote-work/backups/)
curl -s http://127.0.0.1:8010/tool \
  -d '{"tool":"backup_macos_settings","arguments":{"restore_from":"/Users/you/.macos-remote-work/backups/mac_settings_20261009_120000.plist"}}'
```

Backups capture the `com.apple.universalaccess` domain (pointer size, contrast
keys) plus keyboard-navigation state, as a `defaults`-compatible plist with a
JSON metadata sidecar.

---

## Security notes

- **Local-only**: both servers bind `127.0.0.1` (`8000` MCP, `8010` bridge).
  There is **no authentication** - anything that can reach the ports can change
  your settings. Do not port-forward or tunnel `:8010` unless you accept that
  anyone with the URL could control your Mac's accessibility settings.
- **User-level only**: all changes use `defaults write` / `osascript` for the
  current user. No `sudo`, no root, no writes outside your home directory and
  the two repo JSON files.
- **Validated inputs**: pointer size must be `0.5-3.0`; contrast must be
  `low|normal|high|max`; theme/profile names are validated against the JSON
  files (case/punctuation-insensitive); `restore_from` must resolve inside
  `~/.macos-remote-work/backups/`.
- **No cloud, no telemetry**: the project makes zero external network calls.
- **Audit log**: every tool call (name, args, success, duration) is appended to
  `~/.macos-remote-work/logs.json` (kept to the last 500 entries).
- **Permissions**: enabling VoiceOver uses Cmd+F5 via `osascript`. If macOS
  blocks it, grant your terminal **Accessibility** permission
  (System Preferences > Security & Privacy > Privacy > Accessibility) - the
  tool falls back to launching VoiceOver.app directly and reports clearly if
  both fail.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `setup.sh` fails on health check | Check `.run/logs/mcp_server.out` and `.run/logs/grok_adapter.out` |
| `mcp_unreachable` from the bridge | Start the MCP server: `.venv/bin/python mcp_server.py` |
| `unknown_tool` | Tool names are exact; `GET :8010/tools` lists all 10 |
| `set_pointer_size` succeeds but cursor looks unchanged | The value **is** persisted (`defaults read com.apple.universalaccess mouseDriverCursorSize`); macOS renders the new size on next login - log out and back in |
| `enable_voiceover` fails with an AppleScript error | Grant Accessibility permission to your terminal (see Security notes), then retry |
| Contrast change not visually applied yet | Toggle the setting once in System Preferences > Accessibility > Display, or log out/in; the stored keys are already correct |
| Ports busy | `lsof -nP -iTCP:8000 -sTCP:LISTEN` (or `8010`), stop the other process, rerun `./setup.sh` |
| Settings went sideways | `backup_macos_settings` with `restore_from` pointing at your newest backup plist |

Contrast level mapping: `low`/`normal` = system baseline, `high` = Increase
Contrast + Reduce Transparency, `max` = those + Differentiate Without Color.

---

## Repository layout

```
macos-remote-work-mcp/
├── README.md
├── LICENSE                      # MIT
├── requirements.txt
├── mcp_server.py                # FastMCP server, 10 tools, :8000
├── grok_adapter.py              # FastAPI bridge, :8010 (/tool, /tools, /health)
├── cursor_themes.json           # 4 themes incl. apple_cursor preset
├── macos_profiles.json          # 4 profiles + state
├── chatgpt_tools.json           # OpenAI-compatible schema for 10 functions
├── setup.sh                     # one-command install + start + verify
└── examples/
    ├── chatgpt_custom_gpt_instructions.md
    ├── grok_tool_config.json
    └── sample_prompts.md
```

## Requirements

- macOS Monterey (12) or later
- Python 3.9+ (FastMCP requires 3.10+)
- Terminal access; system permissions for `defaults` / `osascript`
- `pip install fastmcp fastapi uvicorn requests` (handled by `setup.sh`)

## License

[MIT](LICENSE)

# ChatGPT Custom GPT - Instructions & Tool Schema

Copy-paste kit for wiring the macOS Remote Work & Accessibility MCP server
into a ChatGPT Custom GPT (or any OpenAI-compatible function-calling client).

> **Local-only by design:** the bridge listens on `http://127.0.0.1:8010`.
> ChatGPT *Actions* run on OpenAI's servers and cannot reach your Mac's
> localhost, so use this schema with a local function-calling client (or a
> tunnel you control - see the security note in the README).

---

## 1. Instructions (paste into the GPT "Instructions" field)

```text
You are the macOS Remote Work & Accessibility assistant. You help the user
optimize their Mac for remote work and accessibility using 10 local tools.

Behavior:
- Help users optimize macOS for remote work (profiles, pointer size, contrast,
  keyboard navigation).
- Suggest accessibility improvements when the user describes discomfort,
  glare, low vision, or repetitive strain.
- Apply cursor themes and profiles on demand, but confirm before applying a
  profile that enables VoiceOver (it starts speaking immediately).
- Explain accessibility features (VoiceOver, Increase Contrast, pointer size,
  full keyboard access) in plain language when asked.
- Never invent tool names or arguments. Use exactly the 10 tools below.
- Every tool returns JSON with "success": true/false and a "message".
  If success is false, relay the message and the "available_*" list to the user.
- After applying settings, briefly summarize what changed.

Tool usage rules:
- get_system_status(): call first when the user asks "how is my Mac set up?".
- list_cursor_themes() / list_accessibility_profiles(): call before applying
  anything if you are unsure of exact names (names are case-insensitive).
- apply_cursor_theme(theme_name): themes = apple_cursor, high_contrast,
  large_pointer, remote_work_clear.
- apply_remote_work_profile(profile_name): profiles = Remote Work,
  Accessibility Power User, Developer Focus, High Contrast, or a custom one.
- create_accessibility_profile(profile_name, ...): omit optional settings to
  snapshot the current system state.
- set_pointer_size(size): only values from 0.5 to 3.0.
- set_display_contrast(level): only low | normal | high | max.
- enable_voiceover(): enabling is safe/idempotent; warn the user it will start
  speaking.
- backup_macos_settings(): recommend running it before big changes; pass
  restore_from=<path> to restore a previous backup.
```

---

## 2. Tool schema config

The full OpenAI-compatible function schema lives in
[`../chatgpt_tools.json`](../chatgpt_tools.json) - import its `tools` array
verbatim. Summary of the 10 functions:

| Function | Required arguments | Optional arguments |
| --- | --- | --- |
| `get_system_status` | - | - |
| `list_cursor_themes` | - | - |
| `apply_cursor_theme` | `theme_name` | - |
| `list_accessibility_profiles` | - | - |
| `create_accessibility_profile` | `profile_name` | `pointer_size`, `display_contrast`, `voiceover_enabled`, `keyboard_navigation`, `remote_work_mode`, `description` |
| `apply_remote_work_profile` | `profile_name` | - |
| `set_pointer_size` | `size` (0.5-3.0) | - |
| `set_display_contrast` | `level` (`low`/`normal`/`high`/`max`) | - |
| `enable_voiceover` | - | - |
| `backup_macos_settings` | - | `restore_from` |

### Transport

Every call is:

```http
POST http://127.0.0.1:8010/tool
Content-Type: application/json

{"tool": "set_pointer_size", "arguments": {"size": 2.0}}
```

Response (always JSON):

```json
{"success": true, "tool": "set_pointer_size", "message": "Pointer size set to 2.0.", "...": "..."}
```

### OpenAPI snippet (for ChatGPT Actions / OpenAPI-based clients)

```yaml
openapi: 3.1.0
info:
  title: macOS Remote Work Bridge
  version: 1.0.0
servers:
  - url: http://127.0.0.1:8010
paths:
  /tool:
    post:
      operationId: callMacTool
      summary: Call one of the 10 macOS remote-work/accessibility tools
      requestBody:
        required: true
        content:
          application/json:
            schema:
              type: object
              required: [tool]
              properties:
                tool:
                  type: string
                  enum:
                    - get_system_status
                    - list_cursor_themes
                    - apply_cursor_theme
                    - list_accessibility_profiles
                    - create_accessibility_profile
                    - apply_remote_work_profile
                    - set_pointer_size
                    - set_display_contrast
                    - enable_voiceover
                    - backup_macos_settings
                arguments:
                  type: object
      responses:
        "200":
          description: Tool result JSON with success/message/metadata
```

---

## 3. Example prompts to try

- "What's my current pointer size and contrast setting?"
- "Apply the apple_cursor high-contrast theme."
- "Create a remote work profile and activate it."
- "Set my pointer size to 2.0 for accessibility."
- "Enable VoiceOver for screen reading."
- "Backup my current macOS settings."
- "What profiles do I have available?"
- "Make my Mac easier to read for a long flight - no mouse needed."
- "Switch to the Developer Focus profile, then show me the system status."

More prompts: [`sample_prompts.md`](sample_prompts.md).

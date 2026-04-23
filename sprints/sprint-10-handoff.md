# Sprint 10 Handoff, Screen Awareness

**Date:** 2026-04-20
**Status:** Complete
**Commits:** 5180da2 (initial), 451f4fc (multi-window + screenshot follow-up)

## What Was Built

Sprint 10 gave Alfred eyes into the user's Windows laptop. When the user asks "what am I looking at?" or drops a vague "save this" reference, Alfred can now pull the foreground window, the active browser URL, and the current clipboard from the laptop over Tailscale and answer specifically.

Three pieces: a new MCP server on the Windows side exposing desktop context, a thin client module on the Jetson that talks to it, and a CLAUDE.md section teaching Alfred when and how to use it.

### Laptop MCP server (Windows side)

Project name `context-mcp`. Server identifies as `DesktopContext v1.27.0`. FastMCP on top of uvicorn, Python. Three tools:

- `get_active_window`: returns `{title, process_name, pid}`. Backed by Win32 calls via ctypes (`GetForegroundWindow`, `GetWindowText`, `GetWindowThreadProcessId`).
- `get_active_url`: returns string or None. Backed by `uiautomation` reading the address bar of the focused Chromium/Firefox window.
- `get_selected_text`: returns string or None. Backed by Windows clipboard via `pywin32`.

Runs on the laptop at a local port, fronted by Tailscale Serve so the Jetson reaches it at `https://<laptop-tailscale-hostname>/mcp` without needing any port exposed to the public internet.

Auto-starts on user login via a VBS script in the Windows Startup folder. Server persists across reboots and sleep cycles without user intervention.

Install location: `C:\Users\<username>\context-mcp\`. See that directory on the laptop for the FastMCP entry point, the requirements file, and the startup VBS.

### core/screen.py (Jetson side, new)

Thin JSON-RPC client for the laptop's MCP, written in the same urllib-only style as the rest of Alfred's core. Three functions:

- `get_screen_state()` returns `{active_window, active_url, clipboard_text, ok}`. `ok` is False when the laptop is unreachable; the three context fields are None.
- `describe_screen()` returns a plain-English sentence. "You're in Chrome looking at github.com/anthropics." Falls back to "You're in {process}, {title}." when no URL. Returns the sentinel "I can't reach your laptop right now." on network failure.
- Internal helpers handle the MCP handshake (initialize + notifications/initialized + tools/call), SSE-or-JSON body parsing, session ID tracking, and `PROCESS_LABELS` for friendly names (chrome.exe -> Chrome, WindowsTerminal.exe -> Windows Terminal, Code.exe -> VS Code, etc.).

Graceful failures: catches HTTPError, URLError, TimeoutError, OSError, RuntimeError, and JSONDecodeError. Never raises to the caller. Voice conversations do not crash if the user's laptop is asleep or off Tailscale.

### CLAUDE.md screen awareness section

New section under "How You Act" tells Alfred:

- Direct triggers: "what am I looking at?", "what's on my screen?", "what tab do I have open?", "what am I reading?", "what's in my clipboard?".
- Context-enriched triggers: "save this", "add this", "remember this page", "what is this". Pull state first, fill in "this" with active_url + active_window title, confirm before writing.
- When not to call: the user on phone, non-laptop topic, recent failure this session.
- Failure mode: read the sentinel, carry on, do not loop.
- Clipboard: do not read long clipboards aloud, summarize or quote the first sentence.

Codebase tree in "Your Own Codebase" updated to list `core/screen.py` and `core/notify.py`.

## Follow-Up: Multi-Window and Screenshot Support (commit 451f4fc)

Landed the same day as the initial sprint. The laptop MCP grew from three tools to five, and `core/screen.py` gained two new client functions plus a plain-English formatter.

### Laptop MCP (five tools now)

All live at `https://<laptop-tailscale-hostname>/mcp`, server name `DesktopContext`.

- `get_active_window`: foreground window only. `{title, process_name, pid}`.
- `get_active_url`: focused browser address bar via `uiautomation`. String or null.
- `get_selected_text`: Windows clipboard via `pywin32`. String or null.
- `get_all_windows` (new): every visible top-level window across every monitor. List of dicts, each with `title, process_name, pid, monitor, x, y, width, height, is_foreground`. Backed by `EnumWindows` + per-monitor `EnumDisplayMonitors` mapping.
- `get_screenshot` (new): base64 PNG of one monitor. Optional `monitor` arg (1=primary, 2=secondary, etc.). Default is primary. Backed by `mss` + `Pillow` encoding. Returns either a bare base64 string or `{monitor, width, height, png_base64}` depending on laptop build; `core/screen.py` handles both shapes.

### core/screen.py additions

- `get_all_windows()`: returns a list of window dicts, or `None` when the laptop is unreachable. Empty list means the MCP responded but nothing is visible.
- `describe_all_windows()`: plain-English spoken summary. "You have N windows open on M monitor(s). Focused is {proc}, {title}. Also open: {list}." Caps the "also open" list at `OTHERS_CAP=6` and adds "and N more" when truncated.
- `get_screenshot(monitor=None)`: returns base64 PNG string on success, or None. Accepts both return shapes from the laptop (bare string vs dict with `png_base64` / `base64` / `data` keys).

Same graceful-failure pattern as the initial three functions: catches HTTPError, URLError, TimeoutError, OSError, RuntimeError, JSONDecodeError, never raises.

### CLAUDE.md routing (tiered)

The "Screen awareness" section now lays out three tiers and when to pick each:

- Default / focused: `describe_screen()` / `get_screen_state()` for "what am I looking at?", "what tab?", "what's in my clipboard?".
- Whole workspace: `describe_all_windows()` / `get_all_windows()` for "what windows do I have open?", "what's on my screens?" (plural), "what am I working on?".
- Pixel-level read: `get_screenshot(monitor=None)` only when titles and URLs are not enough. Save to a temp PNG, describe or quote the specific detail, never dump base64 into voice output.
- Combined: `get_all_windows()` first to pick the right window, then `get_screenshot(monitor=N)` when the user asks about something specific ("the third Chrome tab on my right monitor").

### Verified end-to-end

`claude -p "$(cat current_state.json) what windows do I have open?"` returned:

> "You've got four windows on one monitor. Focused is Windows Terminal on the Jetson. Also open are two Chrome windows, one on Alfred Sprint 7 dev in Claude, the other on your Info Viz grades page, plus Wispr Flow showing status."

Matches the intended spoken vibe.

## Architecture

```
Laptop (Windows, Lenovo, on Tailscale):
  context-mcp server (FastMCP, uvicorn, listens locally)
    polls on demand via Win32 + uiautomation + clipboard APIs
    auto-starts via Startup folder VBS
  Tailscale Serve exposes at https://<laptop-tailscale-hostname>/mcp

Jetson:
  core/screen.py
    POSTs MCP JSON-RPC over HTTPS through Tailscale
    initialize + notifications/initialized + three tools/call per get_screen_state
  Alfred (voice bridge or reflect.py) calls describe_screen / get_screen_state
  as needed during conversations
```

The data does not pass through any cloud service. Everything lives on the tailnet. The MCP runs only while the user is logged into his laptop, so it naturally gates on "is the user present".

## What Works

- "What am I looking at?" answered in one natural sentence. Verified end-to-end via `claude -p`, Alfred replied "Looks like you're in Windows Terminal, working on adding the NYC trip and a DMV appointment to your calendar."
- Context-enriched responses (prompt-driven, not tested in this sprint beyond the CLAUDE.md instruction). Alfred will grab active_url + window title when the user says "save this to my journal" or similar.
- Graceful failure. `LAPTOP_MCP_URL=https://127.0.0.1:1/nowhere python3 -c "from core.screen import describe_screen; print(describe_screen())"` returned the sentinel with no stack trace. `ok` flag in `get_screen_state()` is False on failure.
- Auto-start on laptop login. the user does not need to manually start the MCP each time he boots.
- No cloud exposure. Tailscale Serve is tailnet-only.

## Performance Notes

Each `get_screen_state()` call runs a fresh MCP session: initialize, notifications/initialized, then three tools/call. That is four HTTPS round trips over Tailscale. Measured informally during the sprint at roughly 150 to 250 ms end to end from the Jetson.

Fine for on-demand voice queries where the user is already waiting on a spoken response. Would become a bottleneck if Alfred polled continuously (once per second, say). If that becomes a need later:

- Cache the session ID at module level, reuse across calls, re-initialize on any `Mcp-Session-Id` error. Drops to one round trip per tool call after the first, so `get_screen_state()` goes from ~200 ms to ~80 ms.
- Batch multiple tool calls into a single JSON-RPC `params.calls` array. Not standard MCP, would require a custom tool on the laptop that returns the combined payload.
- Continuous streaming: have the laptop push state to the Jetson over a websocket or SSE pipe on window-focus change events (Windows `SetWinEventHook`). Not needed until Alfred wants an always-on view of what the user is doing.

None of these are worth doing until we actually see latency hurt a conversation.

## What Is Not Built

- **Continuous streaming**. Polling on demand only. No window-switch feed, no timeline of what the user looked at today.
- **Screenshots**. (Landed in the follow-up, commit 451f4fc. See the "Follow-Up" section above.) Originally skipped in the initial commit pending a think on storage and bandwidth; the follow-up kept it stateless (no storage, just base64 over Tailscale on demand).
- **Meeting transcription**. No audio capture or transcription path from the laptop. Natively was considered and rejected as a screen-state source (see Sprint 10 research notes on `alfred-whats-next.md` and the research reply earlier in the sprint conversation).
- **Browser extension fallback for URL**. If uiautomation's address-bar read breaks on a new Chrome version or a new browser, there is no fallback. A 30-line Chrome extension posting to a local port would cover it. Not urgent while uiautomation works.
- **Selection as distinct from clipboard**. `get_selected_text` reads the Windows clipboard, not live selection. If the user highlights without copying, Alfred will not see it. Matches user expectation for now.
- **Session caching**. See Performance Notes. Every call reconstructs the MCP session.

## Smoke Test Results

```
$ curl -sS -X POST https://<laptop-tailscale-hostname>/mcp -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"alfred-jetson","version":"0.1.0"}}}'
event: message
data: {"jsonrpc":"2.0","id":1,"result":{... "serverInfo":{"name":"DesktopContext","version":"1.27.0"}, ...}}

$ python3 /mnt/nvme/alfred/core/screen.py
You're in Windows Terminal, [icon] Add NYC trip and DMV appointment to calendar.
{
  "active_window": {"title": "...", "process_name": "WindowsTerminal.exe", "pid": 21740},
  "active_url": null,
  "clipboard_text": "Sprint 10: Screen Awareness. The laptop MCP server is now live at ...",
  "ok": true
}

$ LAPTOP_MCP_URL=https://127.0.0.1:1/nowhere python3 -c "from core.screen import describe_screen; print(describe_screen())"
I can't reach your laptop right now.

$ cd /mnt/nvme/alfred && claude -p "$(cat current_state.json) what am I looking at right now?" --output-format text
Looks like you're in Windows Terminal, working on adding the NYC trip and a DMV appointment to your calendar.
```

## Laptop Setup Location

All Windows-side code and config lives at `C:\Users\<username>\context-mcp\` on the Lenovo. Contents (from what the sprint produced):

- FastMCP server entry point (Python).
- requirements file with `fastmcp`, `uvicorn`, `pywin32`, `uiautomation`.
- VBS script copied or linked into the Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\`) that launches the server on user login without a console window.
- Tailscale Serve is configured separately via `tailscale serve` on the laptop.

When the laptop needs changes (add a new tool, update dependencies, debug a Chromium URL read that broke), the user edits files in that directory and either reruns the VBS or logs out and back in.

## Recommended Next Sprint

Sprint 11 sits next in the roadmap (Auto-Scheduling). Screen awareness plugs into that sprint naturally: when Alfred proposes a day's schedule and the user is in the middle of something, Alfred can sanity-check ("I see you're deep in VS Code, want me to protect the next hour?") before dropping events onto the calendar.

If Sprint 11 shifts, the next-up Sprint 10 follow-ups worth considering are:

- **Session caching in screen.py** if we notice voice latency.
- **Browser extension for URL** as a reliability safety net.
- **Vision pass on screenshots**. `get_screenshot()` now returns the base64 PNG, but Alfred still needs a decode-and-describe path wired into the voice bridge for "read what's on my screen" to feel seamless. Right now it saves to `/tmp/alfred-screen-*.png` and relies on the model already having vision in its tool-call context.

## File Inventory

```
core/screen.py                          (new, Sprint 10)
CLAUDE.md                               (Screen awareness section, codebase tree)
sprints/sprint-10-handoff.md            (this file)
skills/screen-awareness.md              (Sprint 10)
C:\Users\<username>\context-mcp\             (laptop, outside this repo)
```

No cron changes, no systemd changes on the Jetson side. Windows Startup folder gained one VBS link on the laptop. Tailscale Serve gained one rule on the laptop (not managed from the Jetson).

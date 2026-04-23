---
name: screen-awareness
description: Reference for reading the user's Windows laptop screen state (active window, browser URL, clipboard, all visible windows, screenshots) from the Jetson via the context-mcp server. Covers core/screen.py functions, the five laptop MCP tools, how to add new tools, and Tailscale connectivity requirements.
---

# Screen Awareness

Module: `core/screen.py` on the Jetson. Talks to `context-mcp` (server name `DesktopContext`) running on the user's Windows Lenovo at `https://<laptop-tailscale-hostname>/mcp`, exposed tailnet-only via Tailscale Serve.

## Connectivity

- **Tailnet**: both machines must be on the same Tailscale tailnet. The Jetson is `thoth`, the laptop is `<laptop-hostname>`. Verify with `tailscale status | grep lenovo` from the Jetson.
- **MCP reachability**: `curl -sS -X POST https://<laptop-tailscale-hostname>/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'` should return a JSON-RPC result with `serverInfo.name == "DesktopContext"`.
- **Windows Firewall**: inbound TCP from Tailscale CIDR `100.64.0.0/10` must be allowed on the MCP port. PowerShell: `New-NetFirewallRule -DisplayName "context-mcp Tailscale" -Direction Inbound -Protocol TCP -LocalPort <port> -RemoteAddress 100.64.0.0/10 -Action Allow`. Without this rule, ping works but TCP times out.
- **Auto-start**: the laptop runs `context-mcp` via a VBS script in the Windows Startup folder. The server comes up on user login, not at boot. If the user is logged out, nothing responds.
- **Laptop offline or sleeping**: calls fail with a connection timeout. `get_screen_state()` returns `ok=False`, `describe_screen()` returns the sentinel `"I can't reach your laptop right now."`. Do not retry in a loop.
- **Override URL for testing**: set `LAPTOP_MCP_URL` environment variable to point elsewhere. Useful for local dev or for pointing at a mock server.

## Functions in core/screen.py

### `get_screen_state() -> dict`
Pulls all three laptop tools in sequence and returns a dict:

```python
{
    "active_window": {"title": str, "process_name": str, "pid": int} | None,
    "active_url": str | None,
    "clipboard_text": str | None,
    "ok": bool,
}
```

`ok` is True when the MCP handshake succeeded. `ok=False` means the laptop is unreachable and the three context fields are None. Individual fields can be None on success if the tool legitimately had nothing to return (no browser focused, empty clipboard).

```python
from screen import get_screen_state
state = get_screen_state()
if not state["ok"]:
    say("Your laptop seems to be off or off Tailscale.")
elif state["active_url"]:
    say(f"You're reading {state['active_url']}.")
```

### `describe_screen() -> str`
Convenience wrapper that returns a single plain-English sentence. Runs `get_screen_state()` internally and formats it.

- URL present: `"You're in {process} looking at {short_url}."` (e.g. `"You're in Chrome looking at github.com/anthropics."`).
- No URL, window present: `"You're in {process}, {title}."`.
- No window: `"I can see the laptop but nothing is in the foreground right now."`.
- Unreachable: `"I can't reach your laptop right now."`.

Use this for direct spoken answers to "what am I looking at?". Use `get_screen_state()` when you need raw fields.

### `get_all_windows() -> list | None`
Pulls the full list of visible top-level windows across every monitor via the laptop's `get_all_windows` MCP tool.

Returns a list of dicts, each with:

```python
{
    "title": str,
    "process_name": str,
    "pid": int,
    "monitor": int,          # 1=primary, 2=secondary, etc.
    "x": int, "y": int,
    "width": int, "height": int,
    "is_foreground": bool,
}
```

Return shape conventions:

- List on success, possibly empty when the MCP responded but nothing is on-screen (rare).
- `None` when the laptop is unreachable (matches the `ok=False` pattern of `get_screen_state()`, but as a sentinel value rather than a wrapped dict).

Use this to answer "what windows do I have open?" or to find the right window before calling `get_screenshot()`.

```python
from screen import get_all_windows
windows = get_all_windows()
if windows is None:
    say("Your laptop seems to be off or off Tailscale.")
else:
    chrome_right = [w for w in windows if w["process_name"] == "chrome.exe" and w["monitor"] == 2]
```

### `describe_all_windows() -> str`
Convenience wrapper over `get_all_windows()` that returns a spoken summary. Shape:

- `"You have N windows open on M monitor(s). Focused is {proc}, {title}. Also open: {proc1} ({title1}), {proc2} ({title2}), ..."`
- Others list is capped at `OTHERS_CAP=6` entries, with `"and N more"` appended when truncated.
- Monitor count is inferred from the distinct `monitor` values across returned windows; defaults to "1 monitor" if nothing reports a monitor index.
- Laptop unreachable: returns the sentinel `"I can't reach your laptop right now."`.
- No visible windows (empty list): `"I don't see any visible windows on your laptop right now."`.

Use this for "what's on my screens?" (plural), "what windows do I have open?", "what am I working on?". Keep in mind the summary is mechanical; if the user wants a more natural rundown, call `get_all_windows()` and phrase it yourself.

### `get_screenshot(monitor=None) -> str | None`
Pulls a base64-encoded PNG of one monitor from the laptop's `get_screenshot` MCP tool.

- `monitor=None`: primary monitor (default).
- `monitor=1`: primary. `monitor=2`: secondary. Etc. Monitor indices match what `get_all_windows()` reports.
- Returns the base64 string on success, `None` on failure (laptop unreachable, tool returned an error, unexpected shape).

Handles both laptop response shapes:

- Bare base64 string in the MCP text block.
- Dict with one of `png_base64`, `base64`, or `data` as the payload key.

```python
import base64
from screen import get_screenshot

b64 = get_screenshot(monitor=2)  # right monitor
if b64:
    with open("/tmp/alfred-screen.png", "wb") as f:
        f.write(base64.b64decode(b64))
    # hand /tmp/alfred-screen.png to a vision-capable path
```

Usage rules:

- Never echo the base64 into voice or chat output. Decode to a temp PNG, describe it in one sentence, or extract the specific detail the user asked for.
- Screenshots are heavy. Roughly 300 to 500 KB over the wire per monitor. Only call when titles and URLs are insufficient.
- Prefer `get_all_windows()` first to decide *which* monitor. Do not call `get_screenshot()` speculatively.

### Internal helpers (not for direct use)

- `_post(body, session_id=None)`: POST JSON-RPC, returns `(parsed, sid)`. Handles SSE and JSON response bodies.
- `_initialize()`: runs initialize + notifications/initialized handshake, returns session ID.
- `_call_tool(session_id, name, arguments=None)`: runs `tools/call` and unwraps `structuredContent.result` then `content[0].text`. Tries JSON parse on the text, falls back to returning raw string. `arguments` dict maps to `params.arguments` in the JSON-RPC body; used by `get_screenshot` to pass `{"monitor": N}`.
- `_process_label(process_name)`: maps `chrome.exe` -> `Chrome`, etc. Unknown names fall through minus the `.exe` suffix. Table is `PROCESS_LABELS` at the top of the module.
- `_short_url(url)`: returns `host/firstpath` with `www.` stripped. Keeps voice output tight.

## MCP tools on the laptop

Five tools now, all exposed by `context-mcp` at `https://<laptop-tailscale-hostname>/mcp`. The first three take no arguments; `get_screenshot` takes an optional `monitor` arg.

### `get_active_window`
Returns `{"title": "...", "process_name": "...", "pid": <int>}` as a JSON-encoded string in the MCP response text block. Backed by Win32 calls via ctypes: `GetForegroundWindow`, `GetWindowText`, `GetWindowThreadProcessId`, `OpenProcess`, `GetModuleBaseName`.

### `get_active_url`
Returns a string URL or null. Reads the focused browser's address bar via the `uiautomation` package. Works on Chromium-family browsers (Chrome, Edge, Brave, Arc) and Firefox. Returns null when no browser is focused, when the address bar is empty, or when the UI tree does not expose it (rare on new browser builds).

### `get_selected_text`
Returns the current Windows clipboard contents as a string, or null if the clipboard is empty or holds non-text (image, file list). Backed by `pywin32` clipboard APIs. Reads the clipboard, not the live selection, so the user needs to copy before asking.

### `get_all_windows`
Returns a list of dicts, one per visible top-level window across every monitor. Each dict: `{title, process_name, pid, monitor, x, y, width, height, is_foreground}`. Backed by `EnumWindows` (Win32) to walk the top-level window list, filtered for visible and non-cloaked windows, plus `EnumDisplayMonitors` + `MonitorFromWindow` to assign each window to its monitor index. `is_foreground` matches the window returned by `GetForegroundWindow`.

Takes no arguments. The returned list is not sorted in any guaranteed order; if you need z-order or focused-first, sort on the Jetson side.

### `get_screenshot`
Returns a base64-encoded PNG of one monitor. Optional arg `monitor` (int, default 1=primary). Backed by `mss` for the screen grab and `Pillow` for PNG encoding. The response payload is either a bare base64 string or a dict `{monitor, width, height, png_base64}`; the Jetson client handles both.

Payload size is roughly 300 to 500 KB per monitor at standard 1080p/1440p resolutions. Do not call in loops.

## Common patterns

### Direct screen query (focused window)
the user asks "what am I looking at?", "what tab do I have open?", "what's in my clipboard?". Call `describe_screen()` for the first two. For clipboard specifically, call `get_screen_state()` and summarize or quote the first sentence. Do not read long clipboards aloud verbatim.

### Workspace query (all windows)
the user asks "what windows do I have open?", "what's on my screens?" (plural), "what am I working on?". Call `describe_all_windows()` for a quick spoken answer, or `get_all_windows()` if you want to phrase the summary yourself (more natural, less mechanical).

### Pixel-level read (screenshot)
the user asks "can you see my screen?", "look at my screen", "read what's on this page", "what does this say?", when window titles and URLs clearly will not cover it. Call `get_screenshot(monitor=N)`, decode the base64 to a temp PNG (e.g. `/tmp/alfred-screen.png`), then pull the specific detail he asked about. Do not dump base64 into the voice output.

### Combined (pick window, then read it)
the user asks "what's in the third Chrome tab on my right monitor?" or similar. Call `get_all_windows()` first to pick the right window (filter by process and monitor), then `get_screenshot(monitor=N)` for that monitor's pixels. Say what you saw in one or two sentences.

### Context-enriched action
the user says "save this to my journal", "add this to my tasks", "remember this page". Before writing anything:

1. Call `get_screen_state()`.
2. If `ok=True`, use `active_url` plus `active_window.title` as the referent for "this".
3. State it back: "Saving 'github.com/anthropics/claude-code, Anthropic Claude Code' to your journal. Good?".
4. Wait for "yes", then write.
5. If `ok=False`, ask the user to clarify since you cannot see what he means.

### Ambiguous question
the user asks something that might or might not be about his screen. Cheap guess: pull `get_screen_state()` and let the content inform your answer. Do not mention that you checked. If the screen context turns out irrelevant, ignore it.

### Skip the check
- the user is clearly on his phone (location not at desk, voice bridge session shorter than 30 seconds).
- Non-laptop topic ("did I sleep well?", "what's the weather?").
- Recent `ok=False` this session: honor it and do not keep probing.

## Adding new screen-state tools

The pattern to extend this system:

1. **On the laptop** (`C:\Users\<username>\context-mcp\`), add a new FastMCP tool. Something like:
   ```python
   @mcp.tool()
   def get_open_tabs() -> list[str]:
       """Return URLs of all open tabs in the focused Chromium window."""
       return [...]  # uiautomation walk
   ```
   Rebuild or reload the MCP so the tool is live.

2. **Verify from the Jetson**: `curl -sS -X POST ... -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'` should show the new tool. See the Sprint 10 handoff for the full curl dance.

3. **Wire into screen.py**: add a thin Jetson-side wrapper:
   ```python
   def get_open_tabs() -> list:
       try:
           sid = _initialize()
           return _call_tool(sid, "get_open_tabs") or []
       except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, json.JSONDecodeError):
           return []
   ```

4. **Return shapes**: `_call_tool` prefers `structuredContent.result` over `content[0].text`. If the laptop tool returns a typed result (annotated return type), FastMCP fills `structuredContent` automatically. If it returns plain text, the JSON-parse-then-fallback path in `_call_tool` handles it.

5. **If the new tool takes arguments**: extend `_call_tool` to accept an `arguments` dict and pass it in the `params.arguments` field of the JSON-RPC body. The existing tools take none.

6. **Update CLAUDE.md** with any new trigger phrases.

## Performance and caching

Every `get_screen_state()` call runs a fresh MCP session: initialize, notifications/initialized, then three tools/call. That is four HTTPS round trips over Tailscale, roughly 150 to 250 ms.

Acceptable for on-demand voice queries. If Alfred starts polling this continuously or in loops:

- Cache the session ID at module level. Re-initialize on any `Mcp-Session-Id` error. Drops to one round trip per tool call after the first.
- Batch multiple tool calls by adding a combined tool on the laptop that returns window + URL + clipboard in a single payload.
- Stream focus-change events from the laptop via a websocket instead of polling. Not needed until proven necessary.

## Security and privacy notes

- **Tailnet-only**. No public HTTPS endpoint. Do not remove the Tailscale Serve boundary.
- **Screenshots are tailnet-only and stateless**. `get_screenshot` returns a base64 PNG on demand; nothing is stored on the laptop or the Jetson by default. If Alfred writes one to disk for a vision pass (e.g. `/tmp/alfred-screen-*.png`), treat it as PII: short-lived, never copied into the journal or memory store, never sent to external services beyond the model call that needs it. Do not log screenshots into long-term storage without explicit consent.
- **Clipboard is sensitive**. Passwords, API keys, and private messages all pass through the clipboard. Treat the clipboard field like PII: do not include it in journal writes or long-term memory unless the user explicitly asks, never send it to external services, truncate or summarize in voice output rather than reading verbatim.
- **Laptop gating**. The MCP only runs while the user is logged in. "Is the user at his laptop?" naturally maps to "does this call succeed?". A failure is not an error, it is a signal.

"""Screen awareness via the laptop's MCP server (DesktopContext).

Talks JSON-RPC over MCP streamable HTTP to the Windows laptop at
https://<laptop-tailscale-hostname>/mcp and calls three tools:
get_active_window, get_active_url, get_selected_text. Handles connection
failures gracefully so voice conversations do not crash when the laptop
is off, asleep, or off Tailscale.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

MCP_URL = os.environ.get("LAPTOP_MCP_URL", "https://<laptop-tailscale-hostname>/mcp")
PROTOCOL_VERSION = "2025-06-18"
REQUEST_TIMEOUT_S = 8
UNREACHABLE_MSG = "I can't reach your laptop right now."

# Cache the MCP session id across calls so repeated tool invocations skip the
# initialize + notifications/initialized round trips. Drops a typical
# get_screen_state from four HTTPS round trips to one per tool call.
_SESSION_ID: str | None = None

APP_TITLE_SUFFIXES = [
    " - Google Chrome",
    " - Microsoft Edge",
    " - Mozilla Firefox",
    " - Brave",
    " - Arc",
    " - Visual Studio Code",
    " - Notion",
    " - Slack",
    " - Discord",
    " - Obsidian",
    " - Cursor",
]
OTHERS_CAP = 6

PROCESS_LABELS = {
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "arc.exe": "Arc",
    "code.exe": "VS Code",
    "windowsterminal.exe": "Windows Terminal",
    "explorer.exe": "File Explorer",
    "notion.exe": "Notion",
    "slack.exe": "Slack",
    "obsidian.exe": "Obsidian",
    "spotify.exe": "Spotify",
    "discord.exe": "Discord",
    "teams.exe": "Teams",
    "outlook.exe": "Outlook",
    "powershell.exe": "PowerShell",
    "cmd.exe": "Command Prompt",
    "cursor.exe": "Cursor",
}


def _parse_sse_or_json(raw: str):
    """Parse an MCP response body that may be an SSE event or raw JSON."""
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(raw)


def _post(body: dict, session_id: str = None) -> tuple:
    """POST JSON-RPC. Returns (parsed_or_None, session_id_from_response_header)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
        raw = resp.read().decode("utf-8")
        sid = resp.getheader("Mcp-Session-Id")
    parsed = _parse_sse_or_json(raw) if raw.strip() else None
    return parsed, sid


def _initialize() -> str:
    """Run the MCP initialize handshake. Returns the session id."""
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "alfred-jetson", "version": "0.1.0"},
        },
    }
    _, sid = _post(init_body)
    if not sid:
        raise RuntimeError("MCP server did not return Mcp-Session-Id")
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=sid)
    return sid


def _get_session_id() -> str:
    """Return a cached session id, initializing on first use."""
    global _SESSION_ID
    if _SESSION_ID is None:
        _SESSION_ID = _initialize()
    return _SESSION_ID


def _reset_session() -> None:
    """Drop the cached session so the next call re-initializes."""
    global _SESSION_ID
    _SESSION_ID = None


def _is_session_error(exc: Exception) -> bool:
    """True if the laptop MCP rejected our session id and we should retry."""
    if isinstance(exc, urllib.error.HTTPError):
        # MCP servers typically return 400 or 404 for unknown session ids.
        return exc.code in (400, 401, 404, 410)
    return False


def _call_tool_cached(name: str, arguments: dict | None = None):
    """Call an MCP tool, retrying once with a fresh session on session errors."""
    for attempt in (1, 2):
        try:
            sid = _get_session_id()
            return _call_tool(sid, name, arguments or {})
        except Exception as e:
            if attempt == 1 and _is_session_error(e):
                _reset_session()
                continue
            raise


def _call_tool(session_id: str, name: str, arguments: dict = None):
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }
    parsed, _ = _post(body, session_id=session_id)
    if not parsed:
        return None
    result = parsed.get("result", {})
    if result.get("isError"):
        return None
    structured = result.get("structuredContent")
    if structured and "result" in structured:
        return structured["result"]
    content = result.get("content", [])
    if not content:
        return None
    text = content[0].get("text", "")
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def get_screen_state() -> dict:
    """Return current laptop screen context.

    Keys:
        active_window: {title, process_name, pid} or None
        active_url: string or None
        clipboard_text: string or None
        ok: True when the MCP call succeeded, False when the laptop is unreachable
    """
    try:
        return {
            "active_window": _call_tool_cached("get_active_window"),
            "active_url": _call_tool_cached("get_active_url"),
            "clipboard_text": _call_tool_cached("get_selected_text"),
            "ok": True,
        }
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        RuntimeError,
        json.JSONDecodeError,
    ):
        _reset_session()
        return {
            "active_window": None,
            "active_url": None,
            "clipboard_text": None,
            "ok": False,
        }


def _process_label(process_name: str) -> str:
    if not process_name:
        return "something"
    key = process_name.lower()
    if key in PROCESS_LABELS:
        return PROCESS_LABELS[key]
    return process_name[:-4] if key.endswith(".exe") else process_name


def _short_title(title: str, max_len: int = 60) -> str:
    """Trim trailing app-name suffixes and clip long titles for voice output."""
    if not title:
        return ""
    for suffix in APP_TITLE_SUFFIXES:
        if title.endswith(suffix):
            title = title[: -len(suffix)]
            break
    title = title.strip()
    if len(title) <= max_len:
        return title
    return title[:max_len].rsplit(" ", 1)[0] + "..."


def _short_url(url: str) -> str:
    """Hostname plus first path segment, www stripped, leading dash markers trimmed."""
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, TypeError):
        return url
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in (parsed.path or "").split("/") if p]
    if parts:
        return f"{host}/{parts[0]}"
    return host or url


def describe_screen() -> str:
    """Plain-English sentence Alfred can speak. No markdown, no em dashes."""
    state = get_screen_state()
    if not state["ok"]:
        return UNREACHABLE_MSG
    aw = state["active_window"]
    url = state["active_url"]
    if not aw:
        return "I can see the laptop but nothing is in the foreground right now."
    process = _process_label(aw.get("process_name", ""))
    title = (aw.get("title") or "").strip()
    if url:
        return f"You're in {process} looking at {_short_url(url)}."
    if title:
        return f"You're in {process}, {title}."
    return f"You're in {process}."


def get_all_windows():
    """Call the laptop MCP's get_all_windows tool.

    Returns a list of window dicts on success (possibly empty when nothing
    is on-screen), or None when the laptop is unreachable. Each dict has:
    title, process_name, pid, monitor, x, y, width, height, is_foreground.
    """
    try:
        result = _call_tool_cached("get_all_windows")
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return []
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        RuntimeError,
        json.JSONDecodeError,
    ):
        _reset_session()
        return None


def describe_all_windows() -> str:
    """Plain-English summary of every visible window Alfred can speak."""
    windows = get_all_windows()
    if windows is None:
        return UNREACHABLE_MSG
    if not windows:
        return "I don't see any visible windows on your laptop right now."

    monitor_ids = {w.get("monitor", 0) for w in windows if w.get("monitor")}
    mcount = len(monitor_ids) or 1
    monitor_label = "1 monitor" if mcount == 1 else f"{mcount} monitors"

    parts = [f"You have {len(windows)} windows open on {monitor_label}."]

    foreground = next((w for w in windows if w.get("is_foreground")), None)
    if foreground:
        proc = _process_label(foreground.get("process_name", ""))
        title = _short_title((foreground.get("title") or "").strip())
        if title:
            parts.append(f"Focused is {proc}, {title}.")
        else:
            parts.append(f"Focused is {proc}.")

    others = [w for w in windows if not w.get("is_foreground")]
    if others:
        labels = []
        for w in others:
            proc = _process_label(w.get("process_name", ""))
            title = _short_title((w.get("title") or "").strip())
            labels.append(f"{proc} ({title})" if title else proc)
        if len(labels) > OTHERS_CAP:
            shown = ", ".join(labels[:OTHERS_CAP])
            parts.append(f"Also open: {shown}, and {len(labels) - OTHERS_CAP} more.")
        else:
            parts.append("Also open: " + ", ".join(labels) + ".")

    return " ".join(parts)


def get_screenshot(monitor: int = None):
    """Call the laptop MCP's get_screenshot tool.

    Returns a base64-encoded PNG string on success, or None on failure.
    monitor=None captures the primary monitor; 1=primary, 2=secondary, etc.

    The laptop tool may return either a bare base64 string or a dict with
    {monitor, width, height, png_base64}. Both shapes are handled.
    """
    try:
        args = {} if monitor is None else {"monitor": monitor}
        result = _call_tool_cached("get_screenshot", args)
        if isinstance(result, str) and result:
            return result
        if isinstance(result, dict):
            payload = result.get("png_base64") or result.get("base64") or result.get("data")
            if isinstance(payload, str) and payload:
                return payload
        return None
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        RuntimeError,
        json.JSONDecodeError,
    ):
        _reset_session()
        return None


if __name__ == "__main__":
    print(describe_screen())
    print()
    print(describe_all_windows())
    print()
    print(json.dumps(get_screen_state(), indent=2, default=str))

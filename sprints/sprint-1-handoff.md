# Sprint 1 Handoff: Voice Bridge

**Date completed:** 2026-04-19
**Status:** Shipped and working

---

## What Was Built

A FastAPI voice bridge that connects an iOS Shortcut (triggered by the iPhone action button) to the Claude CLI running on the Jetson Orin Nano. The full flow: hold action button, speak, release, hear Alfred's response through AirPods. End-to-end latency is 4-9 seconds.

### Components

**`/mnt/nvme/alfred/bridge/server.py`**
FastAPI app running under uvicorn on port 8765. Spawns `claude -p` as a subprocess for each request, passing the message on stdin and returning stdout.

**`/etc/systemd/system/alfred-bridge.service`**
systemd unit that runs the bridge as the `thoth` user, restarts on failure, and auto-starts on boot. Working directory is `/mnt/nvme/alfred` so Claude loads the CLAUDE.md context.

**Tailscale serve**
Provides HTTPS termination and tailnet-only access. The bridge is not exposed to the open internet.

---

## Endpoints

| Method | Path | Input | Output | Purpose |
|--------|------|-------|--------|---------|
| POST | `/ask` | Raw text body | Plain text | iPhone Shortcut endpoint |
| GET | `/chat` | `?message=` query param | JSON `{response, duration_ms}` | Quick browser/curl tests |
| POST | `/chat` | JSON `{"message": "..."}` | JSON `{response, duration_ms}` | Programmatic use |
| GET | `/health` |, | `{"ok": true}` | Health check |
| GET | `/test` |, | `{"response": "Alfred is alive"}` | Smoke test |

---

## What Works

- **Action button → AirPods loop is live.** Hold button, speak, release, hear response. Confirmed working on real hardware.
- **Tailscale HTTPS at `https://<jetson-tailscale-hostname>/ask`.** Valid cert, tailnet-only, no port forwarding needed.
- **Plain text in, plain text out on `/ask`.** The Shortcut sends raw dictated text (no JSON wrapping) and gets back only Alfred's words. iOS reads it directly with Speak Text.
- **Full request logging.** Every request logs method, path, all headers, and body to journalctl. Useful for debugging what the iPhone is actually sending.
- **URL decoding on `GET /chat`.** `unquote_plus()` applied so spaces and special characters in query params work correctly.
- **systemd auto-restart.** If the bridge crashes, it comes back within 3 seconds.
- **openclaw-gateway preserved.** The existing `/` → port 18789 Tailscale route was not disturbed. Both services coexist.

---

## What Doesn't Work / Known Issues

**"Invalid HTTP request received" warnings in journalctl**
These come from uvicorn's HTTP parser before requests reach FastAPI. Cause: the iPhone was at some point hitting port 8765 directly over plain HTTP instead of going through Tailscale HTTPS. The iPhone should only use `https://<jetson-tailscale-hostname>/ask`. If these warnings reappear, check the Shortcut URL.

**No authentication**
Any device on the tailnet can hit `/ask` and consume Claude API credits. Acceptable for now since tailnet access is already controlled, but worth revisiting if the tailnet grows.

**No streaming**
Claude runs to completion before the response is returned. The Shortcut hangs for 4-9 seconds with no feedback. Could add a "thinking" chime or visual indicator in the Shortcut, but that's Sprint 2 territory.

**Cold start latency on Claude CLI**
Most of the 4-9 second latency is Claude CLI subprocess startup plus inference, not network. The Tailscale hop adds negligible overhead. This is a Claude CLI architectural constraint; moving to direct API calls (bypassing the CLI) would likely cut 1-2 seconds.

---

## Tailscale Serve Configuration

```
https://<jetson-tailscale-hostname>/
├── /          → http://127.0.0.1:18789  (openclaw-gateway, pre-existing)
├── /ask       → http://localhost:8765/ask
├── /chat      → http://localhost:8765/chat
├── /health    → http://localhost:8765/health
└── /test      → http://localhost:8765/test
```

**Critical gotcha:** `tailscale serve --set-path /ask http://localhost:8765` strips the path before forwarding, so the request arrives at alfred-bridge as `POST /` (404). The backend URL must include the path: `--set-path /ask http://localhost:8765/ask`. This took debugging to discover.

---

## iOS Shortcut Setup

1. **Dictate Text**, captures voice input from the action button press
2. **Get Contents of URL**
   - URL: `https://<jetson-tailscale-hostname>/ask`
   - Method: POST
   - Request Body: Text
   - Body content: the Dictate Text output (raw, no wrapping)
   - Headers: `Content-Type: text/plain`
3. **Speak Text**, reads the response aloud through AirPods

No JSON parsing needed. The response body is Alfred's words, nothing else.

---

## Latency Observations

| Test | Path | Duration |
|------|------|----------|
| Smoke test (direct HTTP) | `POST localhost:8765/ask` | 4083ms |
| End-to-end (via Tailscale HTTPS) | `POST https://thoth.../ask` | 8917ms |

The variance is mostly inference time, not network. "Say the word pineapple" is a trivial prompt; real queries will likely run 6-12 seconds. The Tailscale TLS overhead is negligible.

---

## Technical Notes for Future Work

- The bridge strips `ANTHROPIC_API_KEY` from the subprocess environment so Claude uses the stored interactive login session under `~/.claude/`, not an API key. If the login session expires, the bridge will 500.
- The request logging middleware reads the body and replays it via `request._receive` override so downstream handlers still get the body. This is a FastAPI pattern, don't remove it or `/ask` will receive an empty body.
- The bridge runs as the `thoth` user with `HOME=/home/thoth` set in the service file so Claude finds its config at `~/.claude/`.
- `cwd=ALFRED_HOME` in the subprocess call is what causes Claude to load `CLAUDE.md` as context. If the working directory changes, Alfred loses his identity.

---

## Sprint 2 Candidates

- Move from `claude -p` subprocess to direct Anthropic API calls (lower latency, streaming)
- Add a "processing" chime to the Shortcut so there's audio feedback while waiting
- Persistent conversation sessions so follow-up questions have context
- Wake word detection to remove the action button requirement
- Rate limiting and simple token auth on `/ask`

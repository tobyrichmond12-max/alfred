# Sprint 8 Handoff, Push Notifications

**Date:** 2026-04-20
**Status:** Complete server-side. Client-side intentionally abandoned in favor of Telegram (see Sprint 9 plan).

## What Was Built

Sprint 8 closed the loop on Alfred's proactive output. Before this sprint, reflections and weekly reviews lived only in the vault. Now they can push to the user's phone when something is urgent, and a new briefing command lets Alfred give a natural status summary on demand.

Notifications use a self-hosted ntfy instance on the Jetson. The server, the urgency gating, and the CLAUDE.md instructions are all in and tested. The iOS client-side setup was skipped at the last step: the user decided the ntfy iOS app is not the right long-term channel and will go to Telegram instead (Sprint 9). The infrastructure built here is transport-agnostic enough that the swap to Telegram is cheap.

### ntfy self-hosted on Jetson

```
docker run -d --name ntfy --restart=unless-stopped -p 8088:80 \
  -v /mnt/nvme/alfred/config/ntfy:/var/cache/ntfy \
  binwiederhier/ntfy serve

tailscale serve --bg --https=8443 http://localhost:8088
```

Container is `ntfy` (image `binwiederhier/ntfy:2.21.0`). Data persists at `/mnt/nvme/alfred/config/ntfy`. Host port is 8088, not the originally-spec'd 8080, because `core/server.py` already binds 8080. Tailscale exposes it at `https://<jetson-tailscale-hostname>:8443`, tailnet-only, no public funnel.

Topic: `alfred`. Health endpoint returns `{"healthy":true}` on `/v1/health`.

### core/notify.py (new)

```python
push(message, priority=3, tags=None, title=None) -> bool
push_urgent(message, title=None) -> bool   # priority 5, tags ["warning"]
push_routine(message, title=None) -> bool  # priority 2, tags ["brain"]
```

urllib-only (no new deps), 10-second request timeout, swallows all network errors and returns False. `NTFY_URL` and `NTFY_TOPIC` are env-overridable for testing or when the transport changes (Sprint 9 can flip this without touching callers, though the cleaner move is to replace the module).

### Urgency gating in reflect.py

Added `URGENT_PATTERN` regex covering: `in N min`, `starts at`, `starts in`, `overdue`, `conflict`, `overlap`, `deadline`, `due today`, `due tomorrow`. Case-insensitive, plural-tolerant (no trailing word boundary, so `conflicts`, `overlaps`, `deadlines`, `minutes` all match).

New `push_if_warranted(observations)` runs after `save_reflection` and `append_to_journal`. Decision tree:

- Empty or `Nothing flagged.` -> no push
- URGENT_PATTERN matches -> `push(priority=4, title="Alfred noticed something", tags=["eyes"])`
- Observations exist but no urgent pattern -> `push_routine(title="Alfred reflection")`

The cron log line now includes `push=urgent|routine|none` so you can see from `logs/reflect.log` how each cycle classified.

Graceful degradation: if `notify` fails to import, `push_if_warranted` returns `none` without crashing. reflect.py still writes its files.

### Weekly review push

`generate_weekly_review` now pushes the rendered spoken summary after writing the review file. Priority 3, title "Weekly Review", `calendar` tag. Same soft-fail import pattern.

### "Anything I should know?" briefing command

New section in CLAUDE.md under "How You Act" tells Alfred to combine three sources when asked an open-ended status question:

1. `get_triage_summary()` from triage_todoist
2. `get_calendar_summary(days_back=0, days_forward=7)` from triage_calendar
3. The most recent reflection file (exclude `weekly-review-*.md` and `skill-candidates.md`, sort desc, take top one)

Weave into 3 to 5 sentences. Lead with anything time-sensitive from the reflection, then the calendar shape, then Todoist only if meaningful. End with an open question if something needs a decision. If any source fails, silently degrade.

Also added a "Push notifications" section documenting the ntfy topology and notify.py API so Alfred knows not to push from inside voice conversations.

## Smoke Test Results

```
$ curl -d "test notification from alfred" https://<jetson-tailscale-hostname>:8443/alfred
{"id":"vg2DpMobBtCi", ... "message":"test notification from alfred"}

$ python3 -c "from core.notify import push, push_urgent, push_routine; \
    print(push('Alfred is alive', title='Test', priority=4, tags=['robot_face'])); \
    print(push_urgent('urgency-test', title='Urgent')); \
    print(push_routine('routine-test', title='Routine'))"
True
True
True

$ python3 -c "import sys; sys.path.insert(0, 'core'); from reflect import push_if_warranted; \
    print(push_if_warranted('- <contact-name-c> meeting starts at 1:30\n- 3 overdue tasks'))"
urgent

$ python3 core/weekly_review.py
[2026-04-20T19:42:28-04:00] weekly review saved: .../weekly-review-2026-04-20.md
```

Classifier cases run against 10 synthetic observations all returned the expected `urgent` / `routine` / `none` labels. One regression was caught and fixed mid-sprint: the trailing `\b` dropped "overlaps" as urgent. Removing it resolved without new false positives.

Five test messages are queued on the ntfy server from this sprint's testing. They will be delivered to the first subscriber to connect to topic `alfred`.

## What Works on the Server Side

- Container `ntfy` running and restart-guarded.
- Tailscale rule serving `https://<jetson-tailscale-hostname>:8443` to `localhost:8088`.
- Topic `alfred` accepts POSTs with `Priority`, `Title`, `Tags` headers and returns JSON message confirmations.
- `core/notify.py` sends and fails cleanly.
- `reflect.py` and `weekly_review.py` emit pushes on the correct cadence with correct urgency.
- CLAUDE.md documents the briefing flow and the push topology.

## What Is Not Set Up

- **No iOS ntfy app subscription.** the user decided to go straight to Telegram for the richer channel. The ntfy path works end-to-end on the server but no device is listening yet.
- **No cleanup yet.** The `ntfy` container and the `tailscale serve --https=8443` rule are both still active. Kill them when Sprint 9 lands:
  ```
  docker rm -f ntfy
  rm -rf /mnt/nvme/alfred/config/ntfy
  tailscale serve --https=8443 off
  ```
  Leave them up until Telegram is live so nothing regresses in the meantime.
- **No mobile-reachability test.** Because no client is subscribed, we have not validated that the notification actually travels over cellular-to-Tailscale on the user's iPhone. Not worth chasing now since the transport is changing.
- **Skill-candidate tracking from Sprint 7 is still unverified.** Push notifications were added on top but do not affect that path. Still waiting for a three-peat in the logs.

## Sprint 9 Plan: Telegram Interface

Telegram replaces ntfy as Alfred's second channel. Voice bridge via iOS Shortcut (Action Button) stays for quick hands-free. Telegram becomes the richer channel: structured conversations, voice memos, images, threading by topic, and eventually live voice.

### Scope

1. **Bot creation**: register bot with BotFather, capture token, store in `.env` as `TELEGRAM_BOT_TOKEN`. Capture the user's chat ID (one-time lookup) and store as `TELEGRAM_CHAT_ID`.
2. **Replace notify.py transport**: rewrite `core/notify.py` (or add `core/telegram.py` and make `notify.py` dispatch) so `push/push_urgent/push_routine` send via Telegram's `sendMessage` endpoint. Keep the existing signature so reflect.py and weekly_review.py do not change.
3. **Inbound messages**: long-polling or webhook listener as a new systemd service. Route the user's text messages to the same voice pipeline that handles `/ask` today. Reply in-thread.
4. **Voice memo support**: when Telegram sends a voice note, download the OGG, transcribe (Whisper on the Jetson), feed transcript through the voice pipeline, reply with text and optionally synthesize voice back.
5. **Conversation logging to vault**: every Telegram thread is logged to `vault/conversations/telegram/YYYY/MM/chat-<thread_id>-<date>.md`. Session summaries feed into the same pipeline Alfred already uses for voice bridge sessions.
6. **Thread-per-topic**: Alfred starts separate Telegram topics for calendar, tasks, reflections, weekly reviews, brain-dump. Pushes land in the appropriate topic. the user can reply in-topic.
7. **Cleanup of ntfy**: tear down the container, remove Tailscale serve rule, delete `notify.py` or collapse it into the Telegram adapter.

### Deliverables

- `core/telegram.py` with `send_message`, `send_voice`, `download_voice`, `transcribe` functions.
- `bridge/telegram_listener.py` systemd service (analog of `alfred-bridge`), polling or webhook.
- `core/notify.py` either replaced or kept as a thin dispatch to Telegram.
- Updated CLAUDE.md: new "Telegram interface" section replacing the Push notifications section.
- `skills/telegram-management.md` documenting send, receive, thread, voice-memo flow.
- New cron or hook to auto-summarize Telegram conversations at session close (mirror of voice bridge's 7-minute resume window).

### Open questions for Sprint 9 planning

- Polling vs webhook? Webhook needs a public HTTPS endpoint, which Tailscale does not give us for free. Long-polling is fine for the Jetson and simpler. Default to polling unless we hit latency issues.
- Whisper: which model? `base` runs on CPU fast enough. `small` better accuracy, still tractable. Pick one based on actual dictation quality with the user's voice.
- Thread per topic vs single thread: Telegram supports forum topics in groups. If we make Alfred a solo 1:1 chat, threading is not available. May need a group chat with just Alfred and the user to get topics, or flatten into one stream with inline tags.

### Not in scope for Sprint 9

- Live voice call via Telegram. Moved to Sprint 13 (WebRTC integration, larger project).
- iOS client changes beyond installing Telegram.
- Removing the Action Button voice bridge (stays as the hands-free path).

## File Inventory

```
core/notify.py                 (new, Sprint 8; to be replaced in Sprint 9)
core/reflect.py                (urgency gating, push wiring)
core/weekly_review.py          (weekly push)
CLAUDE.md                      (briefing section, push notifications section)
config/ntfy/                   (new ntfy data dir; delete in Sprint 9 cleanup)
sprints/sprint-8-handoff.md    (this file)
```

Docker: 1 new container (`ntfy`, will be removed in Sprint 9). Tailscale: 1 new serve rule (will be removed in Sprint 9). Cron: no changes.

## Recommended Sprint Order After This

- Sprint 9: Telegram Interface (next, replaces this sprint's client-side)
- Sprint 10: Screen Awareness (shifted from former Sprint 9)
- Sprint 11: Auto-Scheduling
- Sprint 12: Even Realities Integration
- Sprint 13: Live Voice Call via Telegram

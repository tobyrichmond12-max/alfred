# Sprint 4 Handoff: Obsidian Vault, Conversation Logging, Memory System

**Date completed:** 2026-04-20
**Status:** Shipped

---

## What Was Built

Sprint 4 gives Alfred a persistent, human-readable memory. Every voice exchange is now saved as a markdown file. Every day gets a journal note populated with live calendar data, real tasks, and links to every conversation from that day. The Obsidian vault at `/mnt/nvme/alfred/vault/` is the visual frontend, open it in Obsidian to browse Alfred's model of the user as a linked graph of people, preferences, facts, and daily notes.

---

## Vault Structure

**Root:** `/mnt/nvme/alfred/vault/`

```
vault/
├── .obsidian/               # Obsidian config (committed, no secrets)
│   ├── app.json             # Link format, new file location
│   ├── core-plugins.json    # Daily notes, templates, graph, backlinks enabled
│   ├── daily-notes.json     # Points to journal/YYYY/MM/, YYYY-MM-DD format
│   ├── templates.json       # Points to _templates/
│   └── graph.json           # Color groups: journal=green, preference=orange, etc.
├── _templates/
│   ├── daily-note.md        # Table-format template for new daily notes
│   ├── memory.md            # Generic memory note template
│   ├── person.md            # Person note template
│   └── conversation.md      # Conversation log template
├── profile.md               # Root note, the user's identity, links to everything
├── journal/2026/04/         # Daily notes, one per day
├── conversations/2026/04/   # One file per voice exchange
└── memory/
    ├── preferences/         # 5 notes: no-em-dashes, voice-first, direct-comm, etc.
    ├── facts/               # 16 notes: goals, reflections, system facts, co-op
    └── people/              # 4 notes: <advisor-name>, <contact-name-a>, <contact-name-b> + migrated entry
```

`profile.md` is the graph's center of gravity. Every person, preference, and fact links back to it.

---

## Conversation Logging

**File:** `/mnt/nvme/alfred/bridge/server.py`, `log_conversation()`

Every voice call through `/ask` (GET path, GET query, POST) now writes a markdown file to `vault/conversations/YYYY/MM/YYYY-MM-DD-HHMMSS.md` immediately after the response is generated. Format:

```markdown
---
date: 2026-04-20
time: "11:37"
duration_ms: 7155
tags: [conversation, voice]
---

# 2026-04-20 11:37

**the user:** what's my day look like

**Alfred:** You've got Professional Development for Co-op...

---
← [[journal/2026/04/2026-04-20]]
```

The backlink at the bottom of every conversation file points to its daily note. The daily note links forward to all conversations. Graph view shows everything that happened on a given day as a cluster.

---

## Journal Generation

**File:** `/mnt/nvme/alfred/core/journal.py`

Generates daily notes from live state. Runs at midnight via cron. Manual regeneration with an explicit date always force-overwrites.

**Cron entry:**
```
0 0 * * * /usr/bin/python3 /mnt/nvme/alfred/core/journal.py >> /mnt/nvme/alfred/logs/journal.log 2>&1
```

**Sections generated:**

- **Snapshot**: Sleep, HRV (flagged as placeholder until R1 ring is connected), location name
- **Today**: Markdown table of today's calendar events with times and locations
- **Tomorrow**: Markdown table of tomorrow's events (always included, useful context)
- **Tasks**: Overdue items render as an Obsidian `[!warning]` callout block, upcoming items as plain bullets
- **Conversations**: Wikilinks with aliases, `[[path|11:37, what's my day look like]]`, readable in graph and backlink views
- **Alfred's Notes**: Empty section for Alfred to write observations

**Two modes:**
- Explicit date argument → always regenerates the full note
- No argument (cron) → only appends new conversation links to existing notes, preserves manual edits in Alfred's Notes

---

## Memory Migration

**File:** `/mnt/nvme/alfred/core/migrate_memories.py`

One-time script. Migrated all 95 SQLite memories from `memory.db` to individual markdown files routed by type:
- `preference` → `memory/preferences/`
- `relationship` → `memory/people/`
- All others → `memory/facts/`

After migration, pruned 60 Sprint 1 test noise files (testing-testing-123 repeats, fox phrase, hi/hello echoes, generic event records). Kept 16 real facts plus 7 manually curated notes. Added three proper preference stubs referenced by `profile.md`: `no-em-dashes`, `voice-first`, `direct-communication`.

**Vault state after pruning:**
- `memory/facts/`: 16 notes (reflections, goals, system facts, co-op search)
- `memory/people/`: 4 notes (<advisor-name>, <contact-name-a>, <contact-name-b>, migrated entry)
- `memory/preferences/`: 5 notes (3 curated stubs + 2 migrated)

---

## Obsidian Formatting

After initial generation, the journal was reformatted to render cleanly in Obsidian:

- Calendar events use **markdown tables** (not bullet lists)
- Known people names auto-linked as wikilinks (`<contact-name-c> Meeting` → `[[people/<contact-c>|<contact-name-c>]]`)
- Conversation links use **pipe aliases** so they display as readable text in graph/backlinks
- Overdue tasks collapse into an **Obsidian callout block** (`[!warning]+`)
- Location shows human-readable place name; raw GPS coordinates never appear in journal text
- HRV is flagged `*(placeholder)*` when biometrics are not from a real sensor

---

## /location Endpoint + Reverse Geocoding

**File:** `/mnt/nvme/alfred/bridge/server.py`

The `POST /location` endpoint (added in Sprint 3) now reverse-geocodes via Nominatim before writing to `current_state.json`. Stores both the GPS coordinates and a `place` field with the human-readable address. The journal uses `place` for display, never the raw coordinates.

---

## Known Issues Going Into Sprint 5

**Syncthing not set up.** The vault lives on the Jetson but is not synced to Mac or iPad yet. To open in Obsidian on another device, set up Syncthing pointed at `/mnt/nvme/alfred/vault/` on the Jetson and a local folder on the target device.

**Location shows "location unknown."** The smoke test that verified `/location` overwrote the human-readable location fields with raw GPS. Will resolve automatically when the iOS Shortcut fires (reverse geocoder will fill in the place name). To fix immediately: restore `current_state.json` location to `{"city": "Boston", "place": "home", "description": "apartment, at his desk"}`.

**Biometrics are still placeholders.** Sleep, HRV, heart rate are hardcoded from Sprint 1. Flagged in journal output. Waiting on R1 ring or Apple Health integration.

**No <contact-name-c> note in vault.** `<contact-name-c> Meeting` in the calendar auto-links to `[[people/<contact-c>|<contact-name-c>]]` but the file doesn't exist yet, shows as a red link in Obsidian. Create `vault/memory/people/<contact-c>.md` when her role is known.

**Alfred's Notes section never populated automatically.** The section exists and is preserved across regenerations, but nothing writes to it. Sprint 5 candidate: reflective cycle that writes an end-of-day observation after reviewing the day's conversations.

---

## Sprint 5 Candidates

- Set up Syncthing for vault sync to Mac/iPad
- iOS Shortcut for GPS location push (bridge endpoint already ready)
- Biometrics: R1 ring or Apple Health integration
- Automated Alfred's Notes: end-of-day reflective pass over conversation log
- Session continuity (`claude --resume`) for follow-up voice questions
- Create <contact-name-c> people note once her role is clear
- Staleness detection: warn if `current_state.json` is older than 30 minutes

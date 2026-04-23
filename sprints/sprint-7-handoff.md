# Sprint 7 Handoff, Triage and Review

**Date:** 2026-04-20
**Status:** Complete

## What Was Built

Sprint 7 gave Alfred teeth for managing the two biggest daily surfaces: Todoist and Google Calendar. The reflective cycle from Sprint 6 now watches for repeated request patterns and flags skill candidates. A Sunday weekly review closes the loop on each week. Two skill reference docs were added so future Claude Code sessions (and Alfred itself) do not have to rediscover API details.

### core/triage_todoist.py (new)

Walk-through and bulk toolkit for overdue Todoist tasks. Alfred invokes these via Bash `python3 -c` inside voice conversations. Bulk operations pause one second between calls to stay under Todoist's rate limit.

Functions: `get_triage_summary`, `get_overdue_tasks`, `complete_task`, `reschedule_task`, `delete_task`, `bulk_complete`, `bulk_reschedule`. All destructive actions go through Alfred's confirmation loop (see CLAUDE.md). The `_bulk` helper returns `{success, failed, failed_ids}` so partial failures surface cleanly.

### core/triage_calendar.py (new)

Same pattern for Google Calendar. Functions: `get_calendar_summary`, `find_duplicates`, `find_conflicts`, `delete_event`, `bulk_delete`. The summary covers total events, busiest day, empty days, duplicate pairs, and events missing title or location.

Duplicate detection normalizes titles (lowercased, alphanumeric only) and flags pairs whose starts fall within one hour. Weekly recurring classes do not self-trigger because their occurrences land on different days. Conflict detection ignores all-day events on purpose so a day-long "NYC" event does not flag every meeting that day.

Guardrail written into CLAUDE.md: never delete by title match alone. Query first, inspect each event id, confirm with the user. This rule exists because a prior cleanup pass in this sprint over-deleted two legitimate pre-existing class events that shared a title prefix with duplicates. The system denied the auto-restore. Those two events (Investments FINA 3303-02 and Advanced Writing in Business Administration Professions) were later confirmed as cleanup since the semester they belonged to had just ended.

### core/weekly_review.py (new)

Runs Sunday at 7 PM via cron. Pulls tasks completed in the past seven days, still-overdue tasks, past-week calendar events, and coming-week calendar events. Writes `vault/reflections/weekly-review-YYYY-MM-DD.md` with a spoken summary section and four detail sections.

`get_review_summary()` prefers the most recent review file's Spoken Summary section. If no review exists yet, it recomputes live. Alfred reads it Monday morning as the week-opener.

Todoist completions come from the `/tasks/completed/by_completion_date` endpoint. Both `todoist.py` and `triage_todoist.py` do not expose completions, so `weekly_review.py` fetches them directly with the same token.

### Cron entry (new)

```
0 19 * * 0 cd /mnt/nvme/alfred && set -a && . .env && set +a && /usr/bin/python3 core/weekly_review.py >> logs/weekly_review.log 2>&1
```

Sunday 7 PM Eastern. Sources `.env` inline so `TODOIST_API_KEY` is available without relying on the cron shell.

### Skill reference docs (new)

- `skills/calendar-management.md`: covers `gcal.py` and `triage_calendar.py`. Auth, every function signature, all-day vs timed vs recurring creation, an RRULE cookbook with seven common patterns, the restore-from-trash procedure (patch status to confirmed), timezone notes, and cleanup workflow.
- `skills/todoist-management.md`: mirrors the calendar doc for `todoist.py` and `triage_todoist.py`. Auth, filter syntax table, voice-triage flow, by-name lookup, rate limiting, error handling.

Both files have YAML frontmatter (`name`, `description`) so they are discoverable as skills. Both are free of em and en dashes.

### core/reflect.py prompt addition

One bullet added to the "Identify" list in the reflection prompt header: Alfred now watches for repeated asks ("add to calendar", "summarize this thread", "draft a reply to X"). When a pattern crosses three occurrences in a week, Alfred cross-references `vault/reflections/skill-candidates.md` and appends a new entry describing the pattern and sketching what a skill for it would look like. Dedupes against prior mentions.

Lightweight prompt addition only, no new Python glue. The file is created by Alfred on first match.

### bridge/server.py PYTHONPATH

Both the `/chat` and summary-on-timeout subprocess calls now prepend `/mnt/nvme/alfred/core` to `PYTHONPATH` before launching `claude -p`. This lets Alfred write `from triage_todoist import ...` or `from triage_calendar import ...` without sys.path gymnastics, which is exactly the pattern documented in CLAUDE.md for voice triage.

## What Was Fixed

- **`get_tasks` filter bug**: Todoist v1 silently returns every task when you pass `filter=` on `/tasks`. The correct endpoint is `/tasks/filter?query=...`. Both `todoist.get_tasks` and `triage_todoist.get_overdue_tasks` now use the correct form and follow the cursor.
- **CLAUDE.md tool-use instructions**: new sections "Todoist triage", "Calendar triage", and "Weekly review" describe exactly how Alfred invokes each toolkit via Bash `python3 -c`. The "Your Own Codebase" tree was updated to list `skills/` and the new core modules.
- **Em dashes scrubbed**: every file touched this sprint is free of em and en dashes, including the two pre-existing ones in `reflect.py`'s reflection-file header and journal-append format. Prompt in `reflect.py` still bans them as a belt-and-suspenders guard.
- **Calendar duplicates from the first create pass**: four duplicate events created during API iteration on April 20 were removed. Only the intended four remain: NYC (all-day Apr 24), DMV (Apr 24 9 to 10 AM), Investments FINA 3303 (Mon-Thu, May 6 to Jun 18), Advanced Writing in Business (Mon-Thu, May 6 to Jun 18).

## Smoke Test Results

```
$ python3 /mnt/nvme/alfred/core/triage_calendar.py
You have 156 events between 30 days ago and 30 days ahead. Busiest day is
Thursday April 16 with 8 events. 13 days in the window have nothing
scheduled. 38 timed events have no location.

$ python3 -c "from triage_calendar import find_duplicates, find_conflicts; \
    print('dupes:', len(find_duplicates()), 'conflicts:', len(find_conflicts()))"
dupes: 0 conflicts: 9

$ python3 /mnt/nvme/alfred/core/weekly_review.py
[2026-04-20T19:23:42-04:00] weekly review saved:
/mnt/nvme/alfred/vault/reflections/weekly-review-2026-04-20.md
```

First weekly review summary:
- Zero tasks closed this week
- 65 tasks still overdue, oldest from Feb 28 (<example-gig-platform> gig backlog)
- Past week: 25 calendar events
- Coming week: 16 calendar events
- First up: Do my laundry, Monday 7:00 PM

## How to Test Each Feature

Voice commands to try from AirPods:

**Calendar summary**
- "Give me a calendar summary" or "How does my calendar look?"
- Expected: Alfred calls `get_calendar_summary()` and speaks total events, busiest day, empty days, any duplicates or unlabeled events.

**Duplicate detection**
- "Any duplicate events?" or "Check for calendar duplicates"
- Expected: Alfred calls `find_duplicates()`, lists pairs, asks which to delete before any deletion.

**Conflict detection**
- "Any conflicts this week?"
- Expected: `find_conflicts()` returns pairs, Alfred narrates each overlap.

**Event deletion**
- "Delete the event called X" or "Delete that placeholder on Thursday"
- Expected: Alfred queries to find the id, states the delete back, waits for "yes", then calls `delete_event(id)`. For bulk, confirms count first.

**Weekly review on demand**
- "Run my weekly review" or "What did I do this week?"
- Expected: Alfred calls `get_review_summary()`. If no review file exists yet, it recomputes live. Cron will produce fresh ones Sundays at 7 PM.

**Todoist triage walk-through**
- "What's overdue?" (opens with `get_triage_summary()`)
- "Walk me through overdue" (iterates `get_overdue_tasks()` oldest first)
- "Complete everything over a month old" (bucket bulk action, confirms count)

**Skill-candidate tracking**
- No direct command. After three conversations of the same request pattern within a week, the next reflection cycle should append an entry to `vault/reflections/skill-candidates.md`. Verify by tailing that file over the next several days.

## What's Not Done

- **Brain dump flow**: no structured "dump everything in your head, I'll sort it" command. the user has asked for this pattern before but the implementation is not here yet. Candidate is a voice command that opens a free-form capture, then Alfred routes each item to Todoist, calendar, or journal based on shape.
- **Auto-scheduling**: Alfred can create one-off and recurring events via `create_event`, but there is no function that takes a list of flexible tasks plus the user's availability and returns a schedule. Would require a planner that reads `get_calendar_events` and `get_overdue_tasks`, finds open blocks, and suggests time-boxed placements. Confirmation loop still applies.
- **Push notifications**: still deferred from Sprint 6. Reflections and weekly reviews save to the vault only. No ping to the user's phone when a reflection flags something urgent. Candidate: Tailscale-aware push via APNs or a simple Pushover webhook.
- **Reflection retention and weekly review retention**: both accumulate indefinitely. Will need pruning or archival at some point, probably once the vault gets big enough to notice.
- **Skill-candidates file verification**: the prompt instruction is live but unproven. No pattern has crossed three occurrences yet, so the file has not been created. Worth a spot-check once activity accumulates.
- **R1 ring biometrics**: still placeholder, carried over from Sprint 6.
- **Syncthing vault sync**: still not configured, carried over from Sprint 6.

## Recommended Next Sprint

Sprint 8 candidate: **Proactive Alfred**. The reflective cycle and weekly review now produce useful output, but the user only sees it if he asks or opens Obsidian. Next sprint should close that gap.

Three pieces, sized to fit one sprint:

1. **Push notifications**: Pushover or APNs webhook, triggered from `reflect.py` and `weekly_review.py` when observations contain an urgent flag. Silent for routine reflections, loud for "you have a 9 AM you are about to miss."
2. **"Anything I should know?" bridge command**: slash command or keyword that makes Alfred read the most recent reflection aloud. Low-risk, high-value. Already on the Sprint 6 pending list.
3. **Brain dump flow**: voice command opens a free-form capture session. Each thought gets routed to Todoist, calendar, or journal. Confirmation loop per routing decision.

Stretch: R1 ring integration so biometrics stop being placeholders and reflections can factor in sleep and HRV for real.

## File Inventory

```
core/triage_todoist.py                       (new, Sprint 7)
core/triage_calendar.py                      (new, Sprint 7)
core/weekly_review.py                        (new, Sprint 7)
core/reflect.py                              (prompt addition, em-dash cleanup)
core/gcal.py                                 (create_event added)
bridge/server.py                             (PYTHONPATH=core on claude subprocess)
skills/calendar-management.md                (new, Sprint 7)
skills/todoist-management.md                 (new, Sprint 7)
not-now.md                                   (new, first entry: auto-skill discovery)
CLAUDE.md                                    (triage and review sections)
vault/reflections/weekly-review-2026-04-20.md (first review, from smoke test)
sprints/sprint-7-handoff.md                  (this file)
```

Cron additions: 1 line (weekly review Sundays at 7 PM). Systemd changes: none.

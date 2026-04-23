---
name: todoist-management
description: Reference for reading, creating, completing, rescheduling, deleting, and triaging tasks on the user's Todoist via core/todoist.py and core/triage_todoist.py. Covers auth, filter syntax, bulk operations, and voice-triage flow.
---

# Todoist Management

Modules: `core/todoist.py` (create, read) and `core/triage_todoist.py` (overdue walk-through, complete, reschedule, delete, bulk). All Todoist v1 API. Times are America/New_York (Eastern) where relevant.

## Auth

- `TODOIST_API_KEY` must be in the environment. Stored in `/mnt/nvme/alfred/.env`.
- The voice bridge loads `.env` and exports `TODOIST_API_KEY` before invoking `claude -p`, so Alfred has it in-process.
- For terminal or cron invocations, source the env first: `cd /mnt/nvme/alfred && set -a && source .env && set +a`. `triage_todoist.py`'s `__main__` block also loads `.env` directly, so running it as a script works without pre-sourcing.
- `_get_token()` raises `RuntimeError("TODOIST_API_KEY not set in environment")` when the env var is missing.

## Functions in todoist.py

### `create_task(content, due_string=None, priority=1, project_id=None) -> dict`
Create a new task. Returns the full task object from Todoist (includes `id`, `content`, `due`, `priority`, `url`).

- `due_string` accepts natural language: `"tomorrow"`, `"next monday"`, `"in 3 days"`, `"friday 5pm"`. Passed through with `due_lang="en"`.
- `priority`: 1 (none) through 4 (urgent). Only sent when not 1.
- `project_id`: optional Todoist project ID. Omit for Inbox.

```python
from core.todoist import create_task
create_task("Submit co-op application", due_string="friday", priority=3)
```

### `get_tasks(filter_str="today | overdue") -> list`
Fetch tasks matching a Todoist filter expression. Follows pagination via cursor and returns a flat list regardless of result size. Each item is the raw Todoist task dict.

```python
from core.todoist import get_tasks
today_plus_overdue = get_tasks()
high_priority = get_tasks("p1 | p2")
project_specific = get_tasks("##Co-op")
```

### `_get_token() -> str` (internal)
Reads `TODOIST_API_KEY` from env. Raises if unset.

## Functions in triage_todoist.py

### `get_triage_summary() -> str`
Plain-English summary Alfred speaks to open a triage conversation. Total overdue count, oldest task with days past due, breakdown into within-a-week, one-to-four-weeks, over-a-month. Returns "You have no overdue tasks. The pile is clear." when empty.

### `get_overdue_tasks() -> list`
Every overdue task, sorted oldest due date first. Each item: `{id, content, due_date, created_at, project_id}`. Use this to find a task by name or to walk the queue one at a time.

### `complete_task(task_id) -> bool`
Mark a task complete. Returns True on success, False on HTTP or network failure.

### `reschedule_task(task_id, new_due_string) -> bool`
Update a task's due date. Accepts the same natural-language strings as `create_task`.

### `delete_task(task_id) -> bool`
Delete a task permanently. Not recoverable.

### `bulk_complete(task_ids) -> dict`
Complete multiple tasks with a one-second pause between calls. Returns `{'success': N, 'failed': N, 'failed_ids': [...]}`.

### `bulk_reschedule(task_ids, new_due_string) -> dict`
Reschedule multiple tasks to the same due string. Same return shape as `bulk_complete`.

## Filter syntax

Todoist filter expressions used with `get_tasks`:

| Filter | Meaning |
|---|---|
| `today` | Due today |
| `overdue` | Past due |
| `today \| overdue` | Today or overdue (default) |
| `p1` | Priority 1 (urgent) |
| `p1 \| p2` | Urgent or high |
| `no date` | No due date set |
| `##Co-op` | All tasks in the "Co-op" project, subprojects included |
| `#Co-op` | Direct children of "Co-op" only |
| `@waiting` | Tasks with `@waiting` label |
| `search: keyword` | Content contains "keyword" |
| `(today \| overdue) & p1` | Urgent AND (today or overdue) |

## Voice-triage flow

Alfred's destructive actions require confirmation per CLAUDE.md. The triage loop:

1. Open with `get_triage_summary()`. This frames the pile size and oldest item.
2. If the user wants to walk through, call `get_overdue_tasks()` and iterate oldest-first. For each, state the task and how long overdue, then offer four options: keep, reschedule, complete, delete.
3. For bucket requests ("complete everything over a month old", "push last week to Monday"), filter `get_overdue_tasks()` by `due_date` yourself, state the count back for confirmation ("That is 36 tasks, do it?"), then call `bulk_complete` or `bulk_reschedule`.
4. Confirm each destructive action before the call ("Deleting '<example-gig-platform> profile'. Good?"), wait for "yes", then call the single-task function. Confirm completion after ("Done, deleted.").

## By-name lookup

When the user refers to a task by its text ("delete the <example-gig-platform> one", "complete the co-op application task"):

1. Call `get_overdue_tasks()` and look for a case-insensitive substring match on `content`.
2. If multiple match, ask the user which one.
3. If exactly one, state it back with full content and offer the action.
4. Only call `delete_task`, `complete_task`, or `reschedule_task` after "yes".

## Common patterns

**Morning scan**: `get_tasks("today")` for the day's queue. If empty, tell the user the slate is clean. If nonzero, prioritize by `priority` field (higher is more urgent).

**End-of-week cleanup**: `get_overdue_tasks()`, then propose a bulk action on the over-a-month bucket. These are usually dead and the user will confirm bulk-delete or bulk-complete.

**Adding from voice**: `create_task(content, due_string=...)` with the user's natural phrasing. Todoist parses "next monday" and "in 3 days" correctly. Confirm back the parsed due date from the response ("Added, due Monday May 5.").

**Project routing**: call `get_tasks("##ProjectName")` first to discover the `project_id`, then pass it to `create_task` for subsequent adds.

**Weekly review integration**: `weekly_review.py` calls `get_overdue_tasks()` to count the still-overdue pile, and pulls completed tasks via the `/tasks/completed/by_completion_date` endpoint. See `core/weekly_review.py` for the summary format.

## Rate limiting

Todoist's v1 API allows generous request rates for a single user. The 1-second `BULK_DELAY_S` in `triage_todoist` is conservative, well under the documented limits. Do not lower it without cause. Do not run concurrent bulk operations from multiple processes.

## Error handling

`complete_task`, `reschedule_task`, `delete_task` catch `HTTPError` and `URLError` and return False. They do not raise. Check the return value when acting on user intent. Other exceptions (bad token, malformed payload) do propagate. The bulk functions collect failed IDs in the return dict for retry.

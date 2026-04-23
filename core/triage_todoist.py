"""Triage tool for Todoist overdue tasks.

Alfred calls these during voice conversations to walk the user through overdue
tasks one at a time. Bulk operations pause one second between calls to stay
well under Todoist's rate limit.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

from todoist import _get_token

TODOIST_API_BASE = "https://api.todoist.com/api/v1"
EASTERN = ZoneInfo("America/New_York")
BULK_DELAY_S = 1.0


def _request(method: str, path: str, payload: dict = None, query: dict = None):
    url = f"{TODOIST_API_BASE}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"Authorization": f"Bearer {_get_token()}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else None


def get_overdue_tasks() -> list:
    """Return every overdue task, oldest due date first.

    Each item: id, content, due_date, created_at, project_id.
    """
    results = []
    cursor = None
    while True:
        # Todoist v1 uses /tasks/filter?query=... for filter strings.
        # The /tasks?filter= form is silently ignored and returns every task.
        query = {"query": "overdue", "limit": "200"}
        if cursor:
            query["cursor"] = cursor
        resp = _request("GET", "/tasks/filter", query=query)
        if isinstance(resp, list):
            batch, cursor = resp, None
        else:
            batch = resp.get("results", [])
            cursor = resp.get("next_cursor")
        for t in batch:
            due = (t.get("due") or {}).get("date", "") or ""
            results.append({
                "id": t["id"],
                "content": t.get("content", ""),
                "due_date": due[:10],
                "created_at": t.get("created_at") or t.get("added_at") or "",
                "project_id": t.get("project_id", ""),
            })
        if not cursor:
            break
    results.sort(key=lambda r: r["due_date"] or "9999-12-31")
    return results


def get_triage_summary() -> str:
    """Plain-English summary Alfred can speak. No markdown, no em dashes."""
    tasks = get_overdue_tasks()
    if not tasks:
        return "You have no overdue tasks. The pile is clear."

    today = datetime.now(EASTERN).date()

    def days_overdue(t):
        try:
            return (today - date.fromisoformat(t["due_date"])).days
        except (ValueError, TypeError):
            return 0

    within_week = 0
    one_to_four_weeks = 0
    over_a_month = 0
    for t in tasks:
        d = days_overdue(t)
        if d <= 7:
            within_week += 1
        elif d <= 28:
            one_to_four_weeks += 1
        else:
            over_a_month += 1

    oldest = tasks[0]
    oldest_days = days_overdue(oldest)

    parts = [f"You have {len(tasks)} overdue tasks."]
    if oldest_days > 0:
        parts.append(
            f"The oldest is '{oldest['content']}', {oldest_days} days past due "
            f"(due {oldest['due_date']})."
        )

    phrases = []
    if within_week:
        phrases.append(f"{within_week} from the last week")
    if one_to_four_weeks:
        phrases.append(f"{one_to_four_weeks} between one and four weeks old")
    if over_a_month:
        phrases.append(f"{over_a_month} over a month old")
    if phrases:
        parts.append("Breakdown: " + ", ".join(phrases) + ".")

    return " ".join(parts)


def complete_task(task_id: str) -> bool:
    """Mark a single task done. True on success, False on HTTP or network failure."""
    try:
        _request("POST", f"/tasks/{task_id}/close")
        return True
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def reschedule_task(task_id: str, new_due_string: str) -> bool:
    """Update a task's due date. Accepts natural language like 'tomorrow' or 'next monday'."""
    try:
        _request(
            "POST",
            f"/tasks/{task_id}",
            payload={"due_string": new_due_string, "due_lang": "en"},
        )
        return True
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def delete_task(task_id: str) -> bool:
    """Delete a task permanently."""
    try:
        _request("DELETE", f"/tasks/{task_id}")
        return True
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def _bulk(op, task_ids, *args):
    success = 0
    failed_ids = []
    for i, tid in enumerate(task_ids):
        if i > 0:
            time.sleep(BULK_DELAY_S)
        if op(tid, *args):
            success += 1
        else:
            failed_ids.append(tid)
    return {"success": success, "failed": len(failed_ids), "failed_ids": failed_ids}


def bulk_complete(task_ids: list) -> dict:
    """Complete multiple tasks. Returns {'success': N, 'failed': N, 'failed_ids': [...]}."""
    return _bulk(complete_task, task_ids)


def bulk_reschedule(task_ids: list, new_due_string: str) -> dict:
    """Reschedule multiple tasks to the same due string."""
    return _bulk(reschedule_task, task_ids, new_due_string)


if __name__ == "__main__":
    env_file = "/mnt/nvme/alfred/.env"
    if os.path.exists(env_file):
        for line in open(env_file):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    print(get_triage_summary())

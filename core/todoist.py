"""Todoist integration for Alfred."""
import json
import os
import urllib.request
import urllib.error

TODOIST_API_BASE = "https://api.todoist.com/api/v1"


def _get_token() -> str:
    token = os.environ.get("TODOIST_API_KEY")
    if not token:
        raise RuntimeError("TODOIST_API_KEY not set in environment")
    return token


def create_task(content: str, due_string: str = None, priority: int = 1, project_id: str = None) -> dict:
    """Create a task in Todoist. Returns the created task object."""
    payload = {"content": content}
    if due_string:
        payload["due_string"] = due_string
        payload["due_lang"] = "en"
    if priority and priority != 1:
        payload["priority"] = priority
    if project_id:
        payload["project_id"] = project_id

    req = urllib.request.Request(
        f"{TODOIST_API_BASE}/tasks",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {_get_token()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def get_tasks(filter_str: str = "today | overdue") -> list:
    """Fetch tasks matching a Todoist filter string.

    Uses the v1 filter endpoint. Unwraps the paginated response and follows
    the cursor so the return value is a flat list regardless of result size.
    """
    import urllib.parse
    results = []
    cursor = None
    while True:
        params = {"query": filter_str, "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        url = f"{TODOIST_API_BASE}/tasks/filter?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {_get_token()}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, list):
            results.extend(data)
            break
        results.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return results

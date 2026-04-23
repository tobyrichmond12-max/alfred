"""
Canvas LMS integration for Alfred.

Hits the Northeastern Canvas REST API at
`https://canvas.northeastern.edu/api/v1/`.

Auth: personal access token in `CANVAS_API_TOKEN`. Generate one at
Canvas: Account > Settings > New Access Token.

Standard library only: urllib, json, datetime.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("alfred.canvas")

BASE_URL = "https://canvas.northeastern.edu/api/v1"
REQUEST_TIMEOUT = 15


# ---- data model -------------------------------------------------------------

@dataclass
class Assignment:
    id: int
    course_id: int
    course_name: str
    name: str
    due_at: Optional[datetime]
    points_possible: Optional[float]
    html_url: str
    submitted: bool = False
    missing: bool = False

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "course_name": self.course_name,
            "name": self.name,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "points_possible": self.points_possible,
            "html_url": self.html_url,
            "submitted": self.submitted,
            "missing": self.missing,
        }


@dataclass
class CourseGrade:
    course_id: int
    course_name: str
    current_score: Optional[float]
    current_grade: Optional[str]
    final_score: Optional[float]
    final_grade: Optional[str]


@dataclass
class Announcement:
    id: int
    course_id: int
    course_name: str
    title: str
    posted_at: Optional[datetime]
    message_snippet: str
    url: str


# ---- http -------------------------------------------------------------------

def _token() -> str:
    token = os.environ.get("CANVAS_API_TOKEN")
    if not token:
        raise RuntimeError("CANVAS_API_TOKEN is not set")
    return token


def _get(path: str, params: Optional[dict] = None) -> Any:
    """GET a Canvas endpoint with bearer auth. Follows Link-header pagination."""
    results: list[Any] = []
    url: Optional[str] = BASE_URL + path
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT,
                                        context=ssl.create_default_context()) as resp:
                body = json.loads(resp.read().decode())
                link = resp.headers.get("Link", "")
        except urllib.error.URLError as exc:
            log.warning("canvas: %s failed: %s", path, exc)
            break
        if isinstance(body, list):
            results.extend(body)
        else:
            return body
        url = _next_link(link)
    return results


def _next_link(header: str) -> Optional[str]:
    """Parse Canvas Link header and return the next page URL, if any."""
    if not header:
        return None
    for part in header.split(","):
        seg = part.strip()
        if seg.endswith('rel="next"'):
            url = seg.split(";", 1)[0].strip()
            if url.startswith("<") and url.endswith(">"):
                return url[1:-1]
    return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---- courses ----------------------------------------------------------------

def _active_courses() -> list[dict]:
    """Return currently enrolled, active courses."""
    courses = _get("/courses", {
        "enrollment_state": "active",
        "per_page": 50,
        "include[]": "term",
    })
    return [c for c in courses if c.get("id") and not c.get("access_restricted_by_date")]


def _course_name(c: dict) -> str:
    return c.get("name") or c.get("course_code") or f"Course {c.get('id')}"


# ---- assignments ------------------------------------------------------------

def get_upcoming_assignments(days: int = 7) -> list[Assignment]:
    """Fetch assignments due in the next `days` across all active courses."""
    horizon = datetime.now(timezone.utc) + timedelta(days=days)
    now = datetime.now(timezone.utc)
    out: list[Assignment] = []
    for c in _active_courses():
        cid, cname = c["id"], _course_name(c)
        items = _get(f"/courses/{cid}/assignments", {
            "bucket": "upcoming",
            "include[]": "submission",
            "per_page": 50,
            "order_by": "due_at",
        })
        for a in items:
            due = _parse_iso(a.get("due_at"))
            if due and (due < now or due > horizon):
                continue
            submission = a.get("submission") or {}
            out.append(Assignment(
                id=a["id"],
                course_id=cid,
                course_name=cname,
                name=a.get("name", ""),
                due_at=due,
                points_possible=a.get("points_possible"),
                html_url=a.get("html_url", ""),
                submitted=bool(submission.get("submitted_at")),
                missing=bool(submission.get("missing")),
            ))
    out.sort(key=lambda a: (a.due_at or datetime.max.replace(tzinfo=timezone.utc)))
    return out


# ---- grades -----------------------------------------------------------------

def get_grades() -> list[CourseGrade]:
    """Return current grades for every active course."""
    grades: list[CourseGrade] = []
    for c in _active_courses():
        cid = c["id"]
        enrollments = _get(f"/courses/{cid}/enrollments", {
            "user_id": "self",
            "per_page": 10,
        })
        for enr in enrollments:
            grades.append(CourseGrade(
                course_id=cid,
                course_name=_course_name(c),
                current_score=enr.get("current_score"),
                current_grade=enr.get("current_grade"),
                final_score=enr.get("final_score"),
                final_grade=enr.get("final_grade"),
            ))
            break
    return grades


# ---- announcements ----------------------------------------------------------

def get_announcements(days: int = 3) -> list[Announcement]:
    """Fetch announcements posted in the last `days` across active courses."""
    courses = _active_courses()
    context_codes = [f"course_{c['id']}" for c in courses]
    if not context_codes:
        return []
    start = datetime.now(timezone.utc) - timedelta(days=days)
    params: list[tuple[str, str]] = [
        ("start_date", start.strftime("%Y-%m-%d")),
        ("per_page", "50"),
    ]
    for code in context_codes:
        params.append(("context_codes[]", code))
    items = _get("/announcements?" + urllib.parse.urlencode(params))
    by_id = {c["id"]: _course_name(c) for c in courses}
    out: list[Announcement] = []
    for a in items or []:
        course_id = int(str(a.get("context_code", "course_0")).split("_", 1)[-1])
        msg = a.get("message", "") or ""
        snippet = _strip_html(msg)[:280]
        out.append(Announcement(
            id=a.get("id", 0),
            course_id=course_id,
            course_name=by_id.get(course_id, "Course"),
            title=a.get("title", ""),
            posted_at=_parse_iso(a.get("posted_at")),
            message_snippet=snippet,
            url=a.get("html_url", ""),
        ))
    out.sort(key=lambda x: x.posted_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def _strip_html(s: str) -> str:
    """Very lightweight tag stripper for announcement snippets."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.buf: list[str] = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.skip += 1

        def handle_endtag(self, tag):
            if tag in ("script", "style") and self.skip > 0:
                self.skip -= 1

        def handle_data(self, data):
            if self.skip == 0:
                self.buf.append(data)

    p = P()
    p.feed(s)
    return " ".join("".join(p.buf).split())


# ---- summary ----------------------------------------------------------------

def _fmt_due(due: Optional[datetime]) -> str:
    if due is None:
        return "no due date"
    now = datetime.now(timezone.utc)
    delta = due - now
    days = delta.days
    if days < 0:
        return "overdue"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 7:
        return due.astimezone().strftime("%A")
    return due.astimezone().strftime("%b %d")


def get_academic_summary(days: int = 7) -> str:
    """Plain-English briefing line. Combines assignment load and GPA vibe."""
    try:
        assignments = get_upcoming_assignments(days=days)
    except Exception as exc:
        log.exception("canvas: assignment fetch failed")
        return f"Canvas unavailable: {exc}"

    count = len(assignments)
    if count == 0:
        lead = f"No assignments due in the next {days} days."
    else:
        lead = f"{count} assignment{'s' if count != 1 else ''} due this week."
        featured = assignments[0]
        lead += (
            f" {featured.course_name} \"{featured.name}\" "
            f"due {_fmt_due(featured.due_at)}."
        )

    try:
        grades = get_grades()
        scored = [g.current_score for g in grades if g.current_score is not None]
        if scored:
            avg = sum(scored) / len(scored)
            lead += f" Current average {avg:.1f}%."
    except Exception:
        log.exception("canvas: grade fetch failed")

    return lead


# ---- test block -------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys

    if "--offline" in sys.argv:
        # Exercise the pure helpers without hitting Canvas.
        sample_html = "<p>Hello <b>world</b>.</p><script>bad()</script>"
        print("strip_html:", repr(_strip_html(sample_html)))
        now = datetime.now(timezone.utc)
        print("fmt_due today:", _fmt_due(now))
        print("fmt_due tomorrow:", _fmt_due(now + timedelta(days=1)))
        print("fmt_due next week:", _fmt_due(now + timedelta(days=5)))
        print("fmt_due far:", _fmt_due(now + timedelta(days=20)))
        sys.exit(0)

    if not os.environ.get("CANVAS_API_TOKEN"):
        raise SystemExit("set CANVAS_API_TOKEN, or run with --offline")

    print("summary:", get_academic_summary())
    print("upcoming:")
    for a in get_upcoming_assignments():
        print(" ", a.course_name, "-", a.name, "due", _fmt_due(a.due_at))
    print("grades:")
    for g in get_grades():
        print(" ", g.course_name, g.current_score, g.current_grade)

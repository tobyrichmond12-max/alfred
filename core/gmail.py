"""
Gmail integration for Alfred.

Reuses the Google OAuth flow from gcal_auth.py. Expects that module to
expose `get_credentials(scopes: list[str])` returning a credentials
object compatible with `googleapiclient.discovery.build`. The Gmail
scope must be added to the token the first time this module runs,
which will trigger a re-consent on the next `get_credentials()` call.

Scopes required:
    https://www.googleapis.com/auth/gmail.modify

If gcal_auth.py is absent (e.g. running tests off-Jetson), this module
falls back to building credentials from `config/google_credentials.json`
directly and storing the token at `config/gmail_token.json`.

Dependencies: google-api-python-client, google-auth-oauthlib (both
already present on the Jetson). No other pip installs.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("alfred.email")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
DEFAULT_CREDS_PATH = Path("config/google_credentials.json")
DEFAULT_TOKEN_PATH = Path("config/gmail_token.json")


# ---- data model -------------------------------------------------------------

@dataclass
class Email:
    id: str
    thread_id: str
    sender: str
    subject: str
    snippet: str
    date: datetime
    labels: list[str] = field(default_factory=list)
    is_read: bool = True

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "sender": self.sender,
            "subject": self.subject,
            "snippet": self.snippet,
            "date": self.date.isoformat(),
            "labels": list(self.labels),
            "is_read": self.is_read,
        }


# ---- auth -------------------------------------------------------------------

def _service() -> Any:
    """Return an authenticated Gmail API service object."""
    import sys as _sys
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here not in _sys.path:
        _sys.path.insert(0, _here)
    import gcal_auth  # type: ignore
    creds = gcal_auth.get_credentials(GMAIL_SCOPES)
    from googleapiclient.discovery import build  # type: ignore
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ---- parsing helpers --------------------------------------------------------

def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_date(value: str) -> datetime:
    # RFC 2822 dates via email.utils
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _to_email(msg: dict) -> Email:
    headers = (msg.get("payload") or {}).get("headers") or []
    labels = msg.get("labelIds", []) or []
    return Email(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        sender=_header(headers, "From"),
        subject=_header(headers, "Subject"),
        snippet=msg.get("snippet", "").strip(),
        date=_parse_date(_header(headers, "Date")),
        labels=labels,
        is_read="UNREAD" not in labels,
    )


# ---- public API -------------------------------------------------------------

def get_recent_emails(hours: int = 12) -> list[Email]:
    """Fetch emails received in the last `hours` hours, newest first."""
    svc = _service()
    q = f"newer_than:{max(1, int(hours))}h"
    resp = svc.users().messages().list(userId="me", q=q, maxResults=100).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    emails = []
    for mid in ids:
        full = svc.users().messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "Date", "List-Unsubscribe"],
        ).execute()
        emails.append(_to_email(full))
    emails.sort(key=lambda e: e.date, reverse=True)
    return emails


def search_emails(query: str, max_results: int = 50) -> list[Email]:
    """Run an arbitrary Gmail search query, return parsed emails."""
    svc = _service()
    resp = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
    out = []
    for m in resp.get("messages", []):
        full = svc.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        out.append(_to_email(full))
    return out


def archive_emails(email_ids: list[str]) -> int:
    """Remove the INBOX label from each id. Returns count archived."""
    if not email_ids:
        return 0
    svc = _service()
    body = {"ids": list(email_ids), "removeLabelIds": ["INBOX"]}
    svc.users().messages().batchModify(userId="me", body=body).execute()
    return len(email_ids)


def label_email(email_id: str, label: str) -> None:
    """Apply a label by name. Creates the label if it does not exist."""
    svc = _service()
    label_id = _ensure_label(svc, label)
    svc.users().messages().modify(
        userId="me", id=email_id, body={"addLabelIds": [label_id]}
    ).execute()


def _ensure_label(svc: Any, name: str) -> str:
    existing = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in existing:
        if lbl.get("name") == name:
            return lbl["id"]
    created = svc.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


# ---- triage rules -----------------------------------------------------------

NEWSLETTER_SENDER_HINTS = (
    "newsletter", "no-reply", "noreply", "donotreply", "do-not-reply",
    "updates@", "digest@", "hello@", "news@", "bulletin",
    "substack.com", "beehiiv.com", "mailchimp", "campaign-archive",
    "medium.com", "notion.so", "producthunt", "ycombinator",
)

JUNK_KEYWORDS = (
    "% off", "limited time", "ends tonight", "flash sale", "deal of",
    "sale ends", "last chance", "unlock savings", "free trial",
    "act now", "exclusive offer",
)

ACTION_KEYWORDS = (
    "action required", "action needed", "response needed", "please respond",
    "urgent", "deadline", "due ", "due:", "reply by", "rsvp",
    "password reset", "verify your", "confirm your", "invoice",
    "payment", "overdue",
)


def _looks_like_newsletter(email: Email) -> bool:
    sender_l = email.sender.lower()
    if any(h in sender_l for h in NEWSLETTER_SENDER_HINTS):
        return True
    if "CATEGORY_PROMOTIONS" in email.labels or "CATEGORY_UPDATES" in email.labels:
        return True
    return False


def _looks_like_junk(email: Email) -> bool:
    blob = f"{email.subject} {email.snippet}".lower()
    return any(kw in blob for kw in JUNK_KEYWORDS)


def _looks_like_action(email: Email) -> bool:
    blob = f"{email.subject} {email.snippet}".lower()
    return any(kw in blob for kw in ACTION_KEYWORDS)


def auto_triage(emails: list[Email]) -> dict[str, list[Email]]:
    """Bucket emails into action_needed, informational, newsletter, junk.

    Simple rule order, most specific first:
      1. junk keywords in subject/snippet
      2. newsletter sender patterns or Gmail category labels
      3. action keywords
      4. everything else: informational
    """
    buckets: dict[str, list[Email]] = {
        "action_needed": [], "informational": [], "newsletter": [], "junk": [],
    }
    for e in emails:
        if _looks_like_junk(e):
            buckets["junk"].append(e)
        elif _looks_like_newsletter(e):
            buckets["newsletter"].append(e)
        elif _looks_like_action(e):
            buckets["action_needed"].append(e)
        else:
            buckets["informational"].append(e)
    return buckets


# ---- summary helpers --------------------------------------------------------

_DOMAIN_RE = re.compile(r"<([^@>]+@([^>]+))>")


def _sender_label(sender: str) -> str:
    """Pick a short, human friendly label for a sender string."""
    # "Display Name <addr@example.com>"
    if "<" in sender:
        name = sender.split("<", 1)[0].strip().strip('"')
        if name:
            return name
        m = _DOMAIN_RE.search(sender)
        if m:
            return m.group(2)
    if "@" in sender:
        return sender.split("@", 1)[1]
    return sender or "unknown"


def _top_senders(emails: list[Email], n: int = 3) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for e in emails:
        lbl = _sender_label(e.sender)
        counts[lbl] = counts.get(lbl, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:n]


def get_email_summary(hours: int = 12) -> str:
    """Plain-English one-liner for the morning briefing."""
    emails = get_recent_emails(hours=hours)
    if not emails:
        return f"No new emails in the last {hours}h."
    unread = [e for e in emails if not e.is_read]
    buckets = auto_triage(unread or emails)
    parts: list[str] = []
    total = len(unread) if unread else len(emails)
    parts.append(f"{total} new email{'s' if total != 1 else ''}")
    for (name, items) in [
        ("from", _top_senders(unread or emails)),
    ]:
        if items:
            parts.append(
                ", ".join(f"{c} from {label}" for label, c in items)
            )
    tail = []
    if buckets["newsletter"]:
        tail.append(f"{len(buckets['newsletter'])} newsletter{'s' if len(buckets['newsletter']) != 1 else ''}")
    if buckets["junk"]:
        tail.append(f"{len(buckets['junk'])} junk")
    if tail:
        parts.append("(" + ", ".join(tail) + ")")
    return ": ".join(parts[:2]) + (" " + parts[2] if len(parts) > 2 else "")


def get_triage_report(hours: int = 24) -> dict:
    """Structured report for Telegram. Action at top, informational middle,
    junk at bottom with an archive suggestion."""
    emails = get_recent_emails(hours=hours)
    buckets = auto_triage(emails)

    def _line(e: Email) -> str:
        flag = "" if e.is_read else "* "
        return f"{flag}{_sender_label(e.sender)}: {e.subject or '(no subject)'}"

    return {
        "window_hours": hours,
        "total": len(emails),
        "sections": [
            {
                "title": "ACTION NEEDED",
                "items": [_line(e) for e in buckets["action_needed"]],
                "ids": [e.id for e in buckets["action_needed"]],
            },
            {
                "title": "INFORMATIONAL",
                "items": [_line(e) for e in buckets["informational"]],
                "ids": [e.id for e in buckets["informational"]],
            },
            {
                "title": "NEWSLETTERS",
                "items": [_line(e) for e in buckets["newsletter"]],
                "ids": [e.id for e in buckets["newsletter"]],
            },
            {
                "title": "JUNK",
                "items": [_line(e) for e in buckets["junk"]],
                "ids": [e.id for e in buckets["junk"]],
                "offer_archive_all": len(buckets["junk"]) > 0,
            },
        ],
    }


# ---- test block -------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys

    if "--offline" in sys.argv:
        # Exercise the triage rules on fake data without hitting Gmail.
        now = datetime.now(timezone.utc)
        sample = [
            Email("1", "t1", "Canvas <notifications@instructure.com>",
                  "New assignment: Sprint 3", "Canvas notice about Sprint 3...",
                  now, ["INBOX", "UNREAD", "CATEGORY_UPDATES"], False),
            Email("2", "t2", "<advisor> <<advisor>@example.com>",
                  "Action required: review my PR", "Please take a look and respond by Friday",
                  now, ["INBOX", "UNREAD"], False),
            Email("3", "t3", "Substack Digest <digest@substack.com>",
                  "Your weekly digest", "Top posts this week",
                  now, ["INBOX", "CATEGORY_PROMOTIONS"], True),
            Email("4", "t4", "Deals <sales@shop.com>",
                  "50% off ends tonight", "Flash sale ends tonight",
                  now, ["INBOX", "CATEGORY_PROMOTIONS"], True),
            Email("5", "t5", "Advisor <advisor@northeastern.edu>",
                  "Thesis meeting time", "What about Thursday?",
                  now, ["INBOX"], True),
        ]
        buckets = auto_triage(sample)
        for k, items in buckets.items():
            print(f"{k}: {[e.subject for e in items]}")
        print("summary:", _sender_label(sample[1].sender))
        sys.exit(0)

    print(get_email_summary(12))
    print(json.dumps(get_triage_report(24), indent=2, default=str))

"""Financial awareness from Gmail receipts.

scan_financial_emails(hours) -> list[dict]
get_spending_summary(days) -> str
cache_ledger(items) -> None

Categorization is rule-based; no claude call per email.
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALFRED_HOME = "/mnt/nvme/alfred"
_here = os.path.join(ALFRED_HOME, "core")
if _here not in sys.path:
    sys.path.insert(0, _here)

LEDGER_PATH = Path(ALFRED_HOME) / "vault" / "memory" / "finance-ledger.jsonl"

AMOUNT_RE = re.compile(r"\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)")

CATEGORY_RULES = {
    "food": [
        "doordash", "grubhub", "ubereats", "starbucks", "chipotle",
        "dunkin", "panera", "shake shack", "chick-fil", "pizza", "sweetgreen",
    ],
    "transport": [
        "uber", "lyft", "mbta", "amtrak", "gas station",
        "shell", "exxon", "chevron", "citgo", "parking",
    ],
    "subscriptions": [
        "spotify", "netflix", "apple.com/bill", "google one",
        "substack", "github", "chatgpt", "anthropic", "claude",
        "icloud", "hulu", "disney",
    ],
    "shopping": [
        "amazon", "amzn", "ebay", "target.com", "best buy", "walmart",
    ],
}


def _categorize(sender: str, subject: str, snippet: str) -> str:
    blob = f"{sender} {subject} {snippet}".lower()
    for cat, keys in CATEGORY_RULES.items():
        for k in keys:
            if k in blob:
                return cat
    return "other"


def _parse_amount(subject: str, snippet: str) -> float | None:
    for m in AMOUNT_RE.finditer(f"{subject}\n{snippet}"):
        raw = m.group(1).replace(",", "")
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def scan_financial_emails(hours: int = 24) -> list[dict]:
    try:
        from gmail import search_emails  # type: ignore
    except Exception:
        return []
    q = f"newer_than:{max(1, hours // 24 or 1)}d (receipt OR payment OR confirmation OR \"you paid\") category:updates"
    try:
        emails = search_emails(q, max_results=50) or []
    except Exception:
        return []
    out: list[dict] = []
    for e in emails:
        sender = getattr(e, "sender", "") or ""
        subject = getattr(e, "subject", "") or ""
        snippet = getattr(e, "snippet", "") or ""
        amount = _parse_amount(subject, snippet)
        if amount is None:
            continue
        when = getattr(e, "date", None) or datetime.now(timezone.utc)
        out.append({
            "email_id": getattr(e, "id", ""),
            "sender": sender,
            "subject": subject,
            "amount": amount,
            "category": _categorize(sender, subject, snippet),
            "date": when.isoformat() if hasattr(when, "isoformat") else str(when),
        })
    return out


def cache_ledger(items: list[dict]) -> None:
    if not items:
        return
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if LEDGER_PATH.exists():
        for line in LEDGER_PATH.read_text().splitlines():
            try:
                prev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if prev.get("email_id"):
                seen.add(prev["email_id"])
    with LEDGER_PATH.open("a") as f:
        for item in items:
            if item.get("email_id") in seen:
                continue
            f.write(json.dumps(item) + "\n")


def _load_ledger() -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    out = []
    for line in LEDGER_PATH.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _totals(items: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for it in items:
        totals[it.get("category", "other")] += float(it.get("amount", 0))
    return dict(totals)


def get_spending_summary(days: int = 7) -> str:
    items = _load_ledger()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    prior_cutoff = cutoff - timedelta(days=days)
    current = []
    prior = []
    for it in items:
        try:
            ts = datetime.fromisoformat(it.get("date", "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cutoff:
            current.append(it)
        elif ts >= prior_cutoff:
            prior.append(it)

    if not current:
        return "No tracked spending in the window."

    totals = _totals(current)
    total = sum(totals.values())
    parts = [f"You spent ${total:.0f} this week"]
    detail = sorted(totals.items(), key=lambda kv: -kv[1])
    pieces = [f"${v:.0f} {k}" for k, v in detail[:4] if v > 0]
    if pieces:
        parts.append(": " + ", ".join(pieces))
    msg = parts[0] + (parts[1] if len(parts) > 1 else "") + "."

    if prior:
        prior_totals = _totals(prior)
        for cat, cur in totals.items():
            if cur < 10:
                continue
            prev = prior_totals.get(cat, 0)
            if prev > 0 and cur >= prev * 1.4:
                pct = int(round((cur - prev) / prev * 100))
                msg += f" {cat.title()} up {pct}% vs last week."
                break
    return msg


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest", "summary"])
    ns = ap.parse_args()
    if ns.cmd == "selftest":
        now = datetime.now(timezone.utc)
        seeds = [
            {"email_id": "a", "sender": "receipts@doordash.com", "subject": "Your order", "amount": 20, "category": "food", "date": now.isoformat()},
            {"email_id": "b", "sender": "uber.com", "subject": "Thanks for riding", "amount": 15, "category": "transport", "date": now.isoformat()},
            {"email_id": "c", "sender": "amazon.com", "subject": "Order confirmation", "amount": 42, "category": "shopping", "date": (now - timedelta(days=1)).isoformat()},
        ]
        backup = None
        if LEDGER_PATH.exists():
            backup = LEDGER_PATH.with_suffix(".jsonl.bak")
            LEDGER_PATH.rename(backup)
        try:
            cache_ledger(seeds)
            summary = get_spending_summary(7)
            print(summary)
        finally:
            if LEDGER_PATH.exists():
                LEDGER_PATH.unlink()
            if backup and backup.exists():
                backup.rename(LEDGER_PATH)
        print(f"Finance self-test: $77 seeded, summary rendered")
        raise SystemExit(0)
    elif ns.cmd == "summary":
        print(get_spending_summary(7))

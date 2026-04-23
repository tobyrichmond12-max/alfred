"""Prune old reflection files on a retention schedule.

Runs daily via cron after reflect.py settles. Keeps the most recent 30 days
of 3-hourly reflections (`vault/reflections/YYYY-MM-DD-HHMM.md`) and the
most recent 26 weeks of weekly reviews (`vault/reflections/weekly-review-*.md`
plus anything under `vault/reflections/weekly-review-*.md`). Anything older
lands in `vault/reflections/archive/YYYY-MM/` instead of being deleted, so
the signal survives if Alfred needs to look back.

Manual run:
    python3 core/retention.py --dry-run
    python3 core/retention.py
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import date, datetime, timedelta

ALFRED_HOME = "/mnt/nvme/alfred"
VAULT_DIR = os.path.join(ALFRED_HOME, "vault")
REFLECTIONS_DIR = os.path.join(VAULT_DIR, "reflections")
ARCHIVE_DIR = os.path.join(REFLECTIONS_DIR, "archive")

REFLECTION_KEEP_DAYS = 30
WEEKLY_REVIEW_KEEP_DAYS = 26 * 7  # ~6 months

# Preserve the skill-candidates tracker forever; it is a rolling log, not a
# dated artifact.
PROTECTED_NAMES = {"skill-candidates.md"}

REFLECTION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-\d{4}\.md$")
WEEKLY_REVIEW_RE = re.compile(r"^weekly-review-(\d{4}-\d{2}-\d{2})\.md$")


def _parse_date(match: re.Match) -> date | None:
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _classify(fname: str):
    """Return ('reflection' | 'weekly', file_date) or (None, None)."""
    m = REFLECTION_RE.match(fname)
    if m:
        return "reflection", _parse_date(m)
    m = WEEKLY_REVIEW_RE.match(fname)
    if m:
        return "weekly", _parse_date(m)
    return None, None


def _target_archive_path(fname: str, file_date: date) -> str:
    bucket = file_date.strftime("%Y-%m")
    return os.path.join(ARCHIVE_DIR, bucket, fname)


def prune(today: date | None = None, dry_run: bool = False) -> dict[str, int]:
    """Move old reflection/weekly-review files under archive/<YYYY-MM>/.

    Returns a summary: {kept, archived, skipped, weekly_kept, weekly_archived}.
    """
    if today is None:
        today = date.today()
    if not os.path.isdir(REFLECTIONS_DIR):
        return {"kept": 0, "archived": 0, "skipped": 0}

    reflection_cutoff = today - timedelta(days=REFLECTION_KEEP_DAYS)
    weekly_cutoff = today - timedelta(days=WEEKLY_REVIEW_KEEP_DAYS)

    summary = {
        "kept": 0,
        "archived": 0,
        "skipped": 0,
        "weekly_kept": 0,
        "weekly_archived": 0,
    }

    for fname in sorted(os.listdir(REFLECTIONS_DIR)):
        src = os.path.join(REFLECTIONS_DIR, fname)
        if os.path.isdir(src):
            continue
        if fname in PROTECTED_NAMES:
            summary["kept"] += 1
            continue

        kind, file_date = _classify(fname)
        if kind is None or file_date is None:
            summary["skipped"] += 1
            continue

        cutoff = reflection_cutoff if kind == "reflection" else weekly_cutoff
        keep_key = "kept" if kind == "reflection" else "weekly_kept"
        archive_key = "archived" if kind == "reflection" else "weekly_archived"

        if file_date >= cutoff:
            summary[keep_key] += 1
            continue

        dst = _target_archive_path(fname, file_date)
        if dry_run:
            print(f"  would archive {fname} -> {os.path.relpath(dst, VAULT_DIR)}")
        else:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        summary[archive_key] += 1

    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    summary = prune(dry_run=args.dry_run)
    mode = "DRY RUN" if args.dry_run else "applied"
    print(
        f"[retention] {mode}: "
        f"reflections kept={summary['kept']} archived={summary['archived']} "
        f"weekly kept={summary['weekly_kept']} archived={summary['weekly_archived']} "
        f"skipped={summary['skipped']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

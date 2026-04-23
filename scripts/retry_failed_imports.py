#!/usr/bin/env python3
"""Retry conversation extractions that did not land any memory notes.

Looks at vault/imports/claude-export/conversations/ for every conversation
JSON, diffs against the uuids already referenced in vault/memory/, then
re-runs extract_knowledge + write_to_vault on each miss with a 30s cooldown
between calls so Anthropic rate limits do not stack. Progress lands in
logs/retry_imports.log.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import sys
import time

ALFRED_HOME = "/mnt/nvme/alfred"
sys.path.insert(0, os.path.join(ALFRED_HOME, "core"))

from import_claude import (  # noqa: E402
    EXPORT_DIR,
    MEMORY_DIR,
    extract_knowledge,
    parse_conversation,
    write_account_memory,
    write_to_vault,
)

LOG_PATH = os.path.join(ALFRED_HOME, "logs", "retry_imports.log")
SPLIT_DIR = os.path.join(EXPORT_DIR, "conversations")
COOLDOWN_S = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("retry_imports")


def referenced_uuids() -> set[str]:
    """Every conversation uuid already linked from a memory note."""
    uuids: set[str] = set()
    for path in glob.glob(os.path.join(MEMORY_DIR, "**", "*.md"), recursive=True):
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            continue
        for m in re.finditer(r"sources:\s*\[([^\]]+)\]", text):
            for u in re.split(r"[,\s]+", m.group(1)):
                u = u.strip()
                if u:
                    uuids.add(u)
        for m in re.finditer(
            r"imports/claude-export/conversations/[^|\]]+--([a-f0-9]{8})\|", text
        ):
            uuids.add(m.group(1))
    return uuids


def unprocessed_paths(referenced: set[str]) -> list[str]:
    misses: list[str] = []
    for cf in sorted(glob.glob(os.path.join(SPLIT_DIR, "*.json"))):
        try:
            with open(cf) as f:
                conv = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        uuid = conv.get("uuid") or ""
        if not uuid:
            continue
        short = uuid[:8]
        if uuid in referenced or short in referenced:
            continue
        misses.append(cf)
    return misses


def main() -> int:
    # Ensure the bootstrap bio is in place so the extractor can reference it.
    try:
        boot = write_account_memory()
        if boot:
            log.info("bootstrap written: %s", boot)
    except Exception as e:
        log.warning("bootstrap write failed: %s", e)

    refs = referenced_uuids()
    log.info("referenced uuids: %d", len(refs))
    targets = unprocessed_paths(refs)
    log.info("unprocessed conversations: %d", len(targets))

    extract_success = 0
    extract_failed = 0
    items_total = 0

    for i, path in enumerate(targets, 1):
        fname = os.path.basename(path)
        log.info("[%d/%d] extracting %s", i, len(targets), fname)
        try:
            conv = parse_conversation(path)
            items = extract_knowledge(conv)
            if items:
                written = write_to_vault(items)
                items_total += len(items)
                log.info(
                    "[%d/%d] %s -> %d items, %d files touched",
                    i, len(targets), conv.title[:60], len(items), len(written),
                )
            else:
                log.info("[%d/%d] %s -> 0 items", i, len(targets), conv.title[:60])
            extract_success += 1
        except Exception as e:
            extract_failed += 1
            log.error("[%d/%d] %s failed: %s", i, len(targets), fname, e)

        if i < len(targets):
            time.sleep(COOLDOWN_S)

    log.info(
        "done: success=%d failed=%d items=%d",
        extract_success, extract_failed, items_total,
    )
    return 0 if extract_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

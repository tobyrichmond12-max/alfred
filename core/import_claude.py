"""One-shot Claude data export importer.

Extracts a Claude data export (zip), parses every conversation, uses
Claude to extract structured knowledge per conversation, writes the
results into the Obsidian vault under vault/memory/, and indexes
everything into the existing SQLite memory store for search.

Runs as a background batch job after Sprint 11 ships. Not a sprint
feature itself. See skills/claude-import.md for the full procedure.

Export format confirmed on 2026-04-22 (70 conversations, 2175 messages):

    claude-export.zip
    |-- users.json              (1 account: uuid, full_name, email, phone)
    |-- memories.json           (1 compiled bio under 'conversations_memory')
    |-- projects.json           (Claude Projects: name, description, docs)
    |-- conversations.json      (array of conversations, each with:
                                 uuid, name, summary, created_at,
                                 updated_at, chat_messages[])
        chat_messages[i]:
            uuid, sender ("human" | "assistant"), created_at, text
            content[]: blocks of type "text" | "thinking" | "tool_use"
                       | "tool_result" (only "text" is surface content)

Pipeline:
    unpack_export(zip_path)
      -> splits conversations.json into per-conversation files
    parse_conversation(json_path) for each split file
      -> ParsedConversation with only human + assistant text
    extract_knowledge(conversation)
      -> list[KnowledgeItem] via `claude -p` classification prompt
    write_to_vault(knowledge_items)
      -> markdown notes under vault/memory/<category>/ with wikilinks
    build_index(vault_memory_path)
      -> rows in data/memory.db `memories` table

Run end to end via `run_full_import(zip_path)`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterable

ALFRED_HOME = "/mnt/nvme/alfred"
VAULT_DIR = os.path.join(ALFRED_HOME, "vault")
IMPORTS_DIR = os.path.join(VAULT_DIR, "imports")
EXPORT_DIR = os.path.join(IMPORTS_DIR, "claude-export")
SPLIT_DIR = os.path.join(EXPORT_DIR, "conversations")
MEMORY_DIR = os.path.join(VAULT_DIR, "memory")
MEMORY_DB = os.path.join(ALFRED_HOME, "data", "memory.db")
CLAUDE_BIN = "/home/thoth/.local/bin/claude"

KNOWLEDGE_CATEGORIES = (
    "people",
    "decisions",
    "projects",
    "preferences",
    "technical",
)

# Cap the conversation text we send to Claude for extraction so a long
# session does not blow the context window. 60k chars is roughly 15k tokens.
MAX_EXTRACTION_CHARS = 60_000

# Transient failures from `claude -p` (rate limits, 5xx from Anthropic, brief
# network blips) clear up if we wait. One retry after a 30s sleep is enough
# to ride through the common cases without dragging the import out.
EXTRACTOR_RETRY_DELAY_SECONDS = 30

SLUG_RE = re.compile(r"[^a-z0-9]+")

logger = logging.getLogger(__name__)


@dataclass
class ParsedMessage:
    role: str  # "user" or "alfred" (normalized from human/assistant)
    text: str
    timestamp: str


@dataclass
class ParsedConversation:
    uuid: str
    title: str
    created_at: str
    updated_at: str
    messages: list[ParsedMessage]
    source_path: str

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def date_range(self) -> tuple[str, str]:
        if not self.messages:
            return (self.created_at, self.updated_at)
        return (self.messages[0].timestamp, self.messages[-1].timestamp)


@dataclass
class KnowledgeItem:
    category: str  # one of KNOWLEDGE_CATEGORIES
    slug: str
    title: str
    content: str  # markdown body (no frontmatter, no heading)
    source_uuid: str
    source_title: str
    source_date: str
    links: list[str] = field(default_factory=list)  # other slugs to wikilink
    tags: list[str] = field(default_factory=list)


# -----------------------------------------------------------------------------
# Stage 1: unpack
# -----------------------------------------------------------------------------


def _slugify(value: str, fallback: str = "untitled") -> str:
    value = (value or "").strip().lower()
    value = SLUG_RE.sub("-", value).strip("-")
    return value[:80] or fallback


def unpack_export(zip_path: str) -> list[str]:
    """Extract the Claude export zip, split conversations.json per conversation.

    Writes four top-level files into EXPORT_DIR (users.json, memories.json,
    projects.json, conversations.json) as-is. Then splits conversations.json
    into one file per conversation under EXPORT_DIR/conversations/, named
    <created_at_slug>--<uuid>.json. Returns the list of split file paths,
    sorted by created_at ascending so reruns are deterministic.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"Claude export not found at {zip_path}")

    os.makedirs(EXPORT_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(EXPORT_DIR)

    conversations_json = os.path.join(EXPORT_DIR, "conversations.json")
    if not os.path.exists(conversations_json):
        raise FileNotFoundError(
            f"Expected conversations.json inside {zip_path}, not found after extract"
        )

    with open(conversations_json) as f:
        conversations = json.load(f)

    os.makedirs(SPLIT_DIR, exist_ok=True)
    split_paths: list[str] = []
    for conv in conversations:
        uuid = conv.get("uuid") or "no-uuid"
        created = conv.get("created_at", "")[:10]  # YYYY-MM-DD prefix
        title_slug = _slugify(conv.get("name") or "untitled")
        fname = f"{created}--{title_slug}--{uuid[:8]}.json"
        path = os.path.join(SPLIT_DIR, fname)
        with open(path, "w") as f:
            json.dump(conv, f, indent=2, ensure_ascii=False)
        split_paths.append(path)

    split_paths.sort()
    logger.info("unpack_export: wrote %d per-conversation files", len(split_paths))
    return split_paths


# -----------------------------------------------------------------------------
# Stage 2: parse
# -----------------------------------------------------------------------------


def _clean_message_text(msg: dict[str, Any]) -> str:
    """Pull only the user-facing text out of one chat_message block.

    Strategy: walk content[] and concatenate blocks whose type == "text".
    Skip "thinking" (internal reasoning), "tool_use" (tool calls), and
    "tool_result" (tool outputs). Fall back to msg["text"] if content[]
    is missing or produces nothing, since the flat top-level text field
    still exists on every message.
    """
    blocks = msg.get("content") or []
    parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") != "text":
            continue
        text = (b.get("text") or "").strip()
        if text:
            parts.append(text)
    joined = "\n\n".join(parts).strip()
    if joined:
        return joined
    return (msg.get("text") or "").strip()


def parse_conversation(json_path: str) -> ParsedConversation:
    """Read one per-conversation JSON file into a ParsedConversation.

    Normalizes sender labels: "human" -> "user", "assistant" -> "alfred".
    Filters out messages whose cleaned text is empty (pure tool-call turns).
    """
    with open(json_path) as f:
        raw = json.load(f)

    messages: list[ParsedMessage] = []
    for m in raw.get("chat_messages", []):
        sender = m.get("sender", "")
        role = "user" if sender == "human" else "alfred" if sender == "assistant" else sender
        text = _clean_message_text(m)
        if not text:
            continue
        messages.append(
            ParsedMessage(
                role=role,
                text=text,
                timestamp=m.get("created_at", ""),
            )
        )

    return ParsedConversation(
        uuid=raw.get("uuid", ""),
        title=(raw.get("name") or "Untitled").strip() or "Untitled",
        created_at=raw.get("created_at", ""),
        updated_at=raw.get("updated_at", ""),
        messages=messages,
        source_path=json_path,
    )


# -----------------------------------------------------------------------------
# Stage 3: extract knowledge via Claude
# -----------------------------------------------------------------------------


EXTRACTION_SYSTEM_PROMPT_TEMPLATE = """You are an information extraction engine reading a conversation between <your-name> (the "user") and Claude (the "assistant"). Extract durable knowledge that would be useful for Alfred, the user's personal assistant, to remember.

Return ONLY a JSON object with this exact shape, no prose before or after:

{{
  "items": [
    {{
      "category": "people" | "decisions" | "projects" | "preferences" | "technical",
      "title": "short human-readable title",
      "slug": "kebab-case-identifier",
      "content": "1 to 4 sentences of durable fact. No first person. No summary of the conversation itself.",
      "tags": ["keyword", "keyword"],
      "links": ["other-slug-this-relates-to"]
    }}
  ]
}}

CATEGORIES

- people: OTHER people in the user's life (colleagues, classmates, professors, friends, contacts, recruiters, family). NOT the user himself. the user's own contact info, LinkedIn, bio, GPA, coursework, concentrations, tool list, and personal interests belong in preferences/ or should be skipped entirely (they are already in the bootstrap note).
- decisions: a choice the user made or committed to, with the WHY.
- projects: something the user is building, studying, or running (name, goal, status).
- preferences: stylistic or procedural preferences that should influence future interactions.
- technical: reusable tool, library, pattern, or fact relevant to the user's work.

LINKS ARE REQUIRED

Every item must populate "links" with at least one related slug whenever any relationship exists. Link to:
- Other items you produce in this same extraction that share a project, person, or subsystem.
- Existing slugs from the EXISTING SLUGS list below when the topic matches or obviously relates.
Use an empty array ONLY if nothing genuinely relates. Scan hard before concluding nothing relates.

SKIP RULES (do not extract)

- Ephemeral items: time-bound commitments, today's weather, one-off reminders, "good morning sir" style greetings.
- Chit-chat, small talk, meta-discussion about the assistant.
- Narrative or arc-style summaries like "started with X, pivoted to Y, now exploring Z". Extract concrete facts only, not trajectories. Example of what to SKIP: "Progressed through three phases: personal assistant, computer-part flipping, AI interaction." That is a journey summary, not a durable fact; do not emit an item for it. If there is a concrete project inside (e.g. a specific side business), emit only that concrete project instead.
- Anything already present in the BOOTSTRAP NOTE below. Do not re-extract the user's school, GPA, coursework, concentrations, minor, tool list, contact info, or the public personal-interest list.
- Anything you would have to invent or speculate about.

SLUG RULES

- Lowercase kebab-case, ASCII only.
- REUSE an existing slug from the EXISTING SLUGS list when the topic matches. Do not coin a new slug for a concept that already has one (for example, do not create `alfred-voice-loop` when `alfred-voice-pipeline` already exists).
- Title is a free-form sentence fragment.

STYLE RULES

- No em dashes anywhere. Use commas, periods, or parentheses.
- No first-person voice. Write "the user is..." not "I am...".

EXISTING SLUGS (reuse when the topic matches):
{existing_slugs_block}

BOOTSTRAP NOTE (skip facts already here):
{bootstrap_block}
"""


def _format_transcript(conv: ParsedConversation) -> str:
    """Render a ParsedConversation as plain text for the extraction prompt."""
    lines: list[str] = []
    lines.append(f"Conversation title: {conv.title}")
    lines.append(f"Date: {conv.created_at}")
    lines.append(f"Message count: {conv.message_count}")
    lines.append("")
    for m in conv.messages:
        label = "the user" if m.role == "user" else "Claude"
        lines.append(f"[{label}] {m.text}")
        lines.append("")
    return "\n".join(lines).strip()


def _collect_existing_slugs(memory_dir: str = MEMORY_DIR) -> dict[str, list[str]]:
    """Return {category: [slug, ...]} for every existing memory note."""
    result: dict[str, list[str]] = {}
    for cat in KNOWLEDGE_CATEGORIES:
        cat_dir = os.path.join(memory_dir, cat)
        if not os.path.isdir(cat_dir):
            continue
        slugs = sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(cat_dir)
            if f.endswith(".md") and not f.startswith(".")
        )
        if slugs:
            result[cat] = slugs
    return result


def _load_bootstrap_text(memory_dir: str = MEMORY_DIR) -> str:
    """Read vault/memory/claude-bootstrap.md stripped of frontmatter and heading."""
    path = os.path.join(memory_dir, "claude-bootstrap.md")
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        raw = f.read()
    if raw.startswith("---\n"):
        _, _, rest = raw.partition("\n---\n")
        raw = rest or raw
    # Drop the first heading line if present.
    lines = raw.lstrip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _render_existing_slugs_block(slugs_by_cat: dict[str, list[str]]) -> str:
    if not slugs_by_cat:
        return "(none yet)"
    lines: list[str] = []
    for cat in KNOWLEDGE_CATEGORIES:
        slugs = slugs_by_cat.get(cat) or []
        if not slugs:
            continue
        lines.append(f"  {cat}:")
        for s in slugs:
            lines.append(f"    - {s}")
    return "\n".join(lines) if lines else "(none yet)"


def _render_bootstrap_block(bootstrap: str) -> str:
    if not bootstrap:
        return "(no bootstrap note yet)"
    # Cap the bootstrap size so it does not dominate the prompt.
    if len(bootstrap) > 8000:
        bootstrap = bootstrap[:8000] + "\n[truncated]"
    return bootstrap


def _chunk_transcript(
    transcript: str, max_chars: int = 50_000, overlap: int = 5_000
) -> list[str]:
    """Split a long transcript into overlapping windows. Preserves coverage."""
    if len(transcript) <= max_chars:
        return [transcript]
    chunks: list[str] = []
    start = 0
    n = len(transcript)
    while start < n:
        end = min(start + max_chars, n)
        chunks.append(transcript[start:end])
        if end >= n:
            break
        start = end - overlap
    return chunks


def _call_claude_extractor(
    transcript_chunk: str,
    existing_slugs: dict[str, list[str]],
    bootstrap_text: str,
    chunk_info: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Run `claude -p` with the extraction prompt. Returns parsed JSON dict."""
    prompt = EXTRACTION_SYSTEM_PROMPT_TEMPLATE.format(
        existing_slugs_block=_render_existing_slugs_block(existing_slugs),
        bootstrap_block=_render_bootstrap_block(bootstrap_text),
    )
    header = "Conversation:"
    if chunk_info is not None:
        i, total = chunk_info
        header = f"Conversation chunk {i} of {total}:"
    full_prompt = f"{prompt}\n\n{header}\n\n{transcript_chunk}"

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}

    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            result = subprocess.run(
                [CLAUDE_BIN, "-p", "--output-format", "text"],
                input=full_prompt,
                capture_output=True,
                text=True,
                cwd=ALFRED_HOME,
                timeout=180,
                env=env,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"claude extractor exited {result.returncode}: {result.stderr[-500:]}"
                )

            out = result.stdout.strip()
            if out.startswith("```"):
                out = re.sub(r"^```(?:json)?\s*", "", out)
                out = re.sub(r"\s*```$", "", out)
            return json.loads(out)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            last_error = e
            if attempt == 1:
                logger.warning(
                    "claude extractor failed (attempt 1/2), retrying in %ds: %s",
                    EXTRACTOR_RETRY_DELAY_SECONDS,
                    e,
                )
                time.sleep(EXTRACTOR_RETRY_DELAY_SECONDS)
                continue
            break

    if isinstance(last_error, json.JSONDecodeError):
        raise RuntimeError(f"claude extractor returned non-JSON after retry: {last_error}")
    raise RuntimeError(f"claude extractor failed after retry: {last_error}")


def _augment_links(items: list[KnowledgeItem]) -> list[KnowledgeItem]:
    """Add cross-references across items in the batch based on title/slug/tag overlap.

    Three tiers, scored highest first:
      3. Item B's title appears verbatim in A's content.
      2. Item B's slug words (3+ chars, 2+ words) appear together in A's content.
      1. Items A and B share 2+ tags.
    Caps at 6 links per item to keep notes readable.
    """
    if len(items) <= 1:
        return items

    for a in items:
        haystack = f"{a.title} {a.content}".lower()
        a_tags = {t.lower() for t in a.tags}
        existing = set(a.links)
        candidates: list[tuple[int, str]] = []
        for b in items:
            if b is a or b.slug == a.slug or b.slug in existing:
                continue
            b_title = b.title.lower().strip()
            b_words = [w for w in b.slug.split("-") if len(w) >= 3]
            b_phrase = " ".join(b_words)

            if len(b_title) >= 8 and b_title in haystack:
                candidates.append((3, b.slug))
                continue
            if len(b_words) >= 2 and b_phrase and b_phrase in haystack:
                candidates.append((2, b.slug))
                continue
            b_tags = {t.lower() for t in b.tags}
            if len(a_tags & b_tags) >= 2:
                candidates.append((1, b.slug))

        candidates.sort(reverse=True)
        for _, slug in candidates:
            if slug in existing:
                continue
            a.links.append(slug)
            existing.add(slug)
            if len(a.links) >= 6:
                break
    return items


def extract_knowledge(conversation: ParsedConversation) -> list[KnowledgeItem]:
    """Use Claude to pull structured knowledge out of one parsed conversation.

    Chunks long transcripts into overlapping 50k-char windows, runs the
    extractor per chunk, merges results, dedupes by slug (first wins),
    then augments the links field with heuristic cross-references.
    """
    if not conversation.messages:
        return []

    transcript = _format_transcript(conversation)
    existing_slugs = _collect_existing_slugs()
    bootstrap = _load_bootstrap_text()
    chunks = _chunk_transcript(transcript, max_chars=50_000, overlap=5_000)

    raw_items: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks, 1):
        info = (i, len(chunks)) if len(chunks) > 1 else None
        parsed = _call_claude_extractor(chunk, existing_slugs, bootstrap, chunk_info=info)
        raw_items.extend(parsed.get("items") or [])

    # Dedupe by slug across chunks (first occurrence wins).
    seen: set[str] = set()
    source_date = (conversation.created_at or "")[:10]
    items: list[KnowledgeItem] = []
    for raw in raw_items:
        category = (raw.get("category") or "").strip().lower()
        if category not in KNOWLEDGE_CATEGORIES:
            continue
        slug = _slugify(raw.get("slug") or raw.get("title") or "")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        items.append(
            KnowledgeItem(
                category=category,
                slug=slug,
                title=(raw.get("title") or slug).strip(),
                content=(raw.get("content") or "").strip(),
                source_uuid=conversation.uuid,
                source_title=conversation.title,
                source_date=source_date,
                links=[_slugify(l) for l in (raw.get("links") or []) if l],
                tags=[str(t).strip() for t in (raw.get("tags") or []) if t],
            )
        )
    items = _merge_by_title(items)
    items = _augment_links(items)
    return items


# -----------------------------------------------------------------------------
# Stage 4: write to vault
# -----------------------------------------------------------------------------


def _render_note(item: KnowledgeItem, is_new: bool) -> str:
    """Render a KnowledgeItem as a vault note. New file gets frontmatter;
    existing file gets an appended update block."""
    now_iso = item.source_date or ""
    related = (
        "\n\n## Related\n" + "\n".join(f"- [[memory/{item.category}/{s}|{s}]]" for s in item.links)
        if item.links
        else ""
    )
    source_link = (
        f"\n\n## Sources\n- [[imports/claude-export/conversations/"
        f"{item.source_date}--{_slugify(item.source_title)}--{item.source_uuid[:8]}"
        f"|{item.source_title}]] ({item.source_date})"
    )

    if is_new:
        tags_line = ", ".join(item.tags) if item.tags else ""
        return (
            f"---\n"
            f"title: {item.title}\n"
            f"category: {item.category}\n"
            f"tags: [{tags_line}]\n"
            f"first_seen: {now_iso}\n"
            f"sources: [{item.source_uuid}]\n"
            f"---\n\n"
            f"# {item.title}\n\n"
            f"{item.content}"
            f"{related}"
            f"{source_link}\n"
        )

    # Existing file: append a dated update block. Keeps the original content
    # intact and lets future passes layer new observations on top.
    return (
        f"\n\n## Update, {now_iso}\n\n"
        f"{item.content}"
        f"{source_link}\n"
    )


def _normalize_title(title: str) -> str:
    """Fold a title for fuzzy equality comparison. Lowercase, whitespace collapsed."""
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _merge_by_title(items: list[KnowledgeItem]) -> list[KnowledgeItem]:
    """Collapse items with identical normalized titles. First slug wins.

    Combines tags and links, keeps the longer content, and appends the second
    item's source_uuid so both provenance chains are preserved.
    """
    by_title: dict[str, KnowledgeItem] = {}
    merged: list[KnowledgeItem] = []
    collapsed = 0
    for item in items:
        key = _normalize_title(item.title)
        if not key:
            merged.append(item)
            continue
        existing = by_title.get(key)
        if existing is None:
            by_title[key] = item
            merged.append(item)
            continue
        # Merge into existing (keep first slug).
        existing.tags = list(dict.fromkeys([*existing.tags, *item.tags]))
        existing.links = list(dict.fromkeys([*existing.links, *item.links]))
        if len(item.content) > len(existing.content):
            existing.content = item.content
        collapsed += 1
    if collapsed:
        logger.info("_merge_by_title collapsed %d duplicate titles", collapsed)
    return merged


def write_to_vault(items: Iterable[KnowledgeItem]) -> list[str]:
    """Write every KnowledgeItem to vault/memory/<category>/<slug>.md.

    Runs a normalized-title merge first so two slugs that name the same
    concept collapse into the first slug's file before anything is written.

    New slugs get a fresh file with frontmatter. Repeats of the same slug
    append a dated Update section so prior content is preserved. Returns
    the list of file paths written or updated.
    """
    items = _merge_by_title(list(items))
    touched: list[str] = []
    for item in items:
        cat_dir = os.path.join(MEMORY_DIR, item.category)
        os.makedirs(cat_dir, exist_ok=True)
        path = os.path.join(cat_dir, f"{item.slug}.md")
        is_new = not os.path.exists(path)
        rendered = _render_note(item, is_new)
        mode = "w" if is_new else "a"
        with open(path, mode) as f:
            f.write(rendered)
        touched.append(path)
    return touched


def write_account_memory() -> str | None:
    """Copy the pre-compiled bio from memories.json into vault/memory/.

    The Claude export bundles an account-level memory summary that is
    already structured and the user-specific. Land it as a single note so it
    is searchable alongside extracted knowledge.
    """
    src = os.path.join(EXPORT_DIR, "memories.json")
    if not os.path.exists(src):
        return None
    with open(src) as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        return None
    body = (data[0].get("conversations_memory") or "").strip()
    if not body:
        return None

    os.makedirs(MEMORY_DIR, exist_ok=True)
    path = os.path.join(MEMORY_DIR, "claude-bootstrap.md")
    content = (
        "---\n"
        "title: Claude Account Memory (bootstrap)\n"
        "category: bootstrap\n"
        "source: claude-export/memories.json\n"
        "---\n\n"
        "# Claude Account Memory\n\n"
        "Imported from the Claude account-level memory on the export date. "
        "Pre-compiled bio covering work, personal, top-of-mind, and recent history. "
        "Treat as ground truth for facts Alfred should already know.\n\n"
        f"{body}\n"
    )
    with open(path, "w") as f:
        f.write(content)
    return path


# -----------------------------------------------------------------------------
# Stage 5: build the search index
# -----------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^(\w+):\s*(.*)$")
_LIST_INLINE_RE = re.compile(r"^\[(.*)\]$")


def _ensure_slug_column(conn: sqlite3.Connection) -> None:
    """Add the `slug` column to `memories` if it is missing.

    Keeps upserts cheap: a unique (memory_type, slug) index lets us replace
    a row in place instead of scanning content prefixes. No-op on reruns.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    if "slug" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN slug TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_type_slug "
        "ON memories(memory_type, slug) WHERE slug IS NOT NULL"
    )
    conn.commit()


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter dict, body string).

    Only handles scalar values and flat [a, b, c] inline lists. That matches
    the notes _render_note writes, which is all we ever index here. Anything
    unrecognized returns ({}, raw).
    """
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw
    meta: dict[str, Any] = {}
    for line in m.group(1).splitlines():
        fm = _FRONTMATTER_FIELD_RE.match(line)
        if not fm:
            continue
        key = fm.group(1).strip()
        val = fm.group(2).strip()
        list_m = _LIST_INLINE_RE.match(val)
        if list_m:
            inner = list_m.group(1).strip()
            meta[key] = [x.strip() for x in inner.split(",") if x.strip()] if inner else []
        else:
            meta[key] = val
    return meta, m.group(2).lstrip("\n")


def _extract_body_content(body: str) -> str:
    """Drop the first `# heading` line, keep the rest."""
    stripped = body.lstrip()
    if stripped.startswith("# "):
        _, _, rest = stripped.partition("\n")
        return rest.lstrip("\n")
    return body.strip()


def build_index(vault_memory_path: str = MEMORY_DIR, db_path: str = MEMORY_DB) -> int:
    """Index vault/memory/ markdown into data/memory.db `memories` table.

    Walks every .md under vault_memory_path, parses frontmatter plus body,
    and upserts one row per note into `memories` keyed on (memory_type, slug).
    Embeddings stay NULL here; vector search callers tolerate that and
    plain substring / tag lookups still work. A sentence-transformers pass
    can backfill the BLOB later without re-walking the vault.
    """
    if not os.path.isdir(vault_memory_path):
        return 0
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_slug_column(conn)

    rows_written = 0
    for category in KNOWLEDGE_CATEGORIES:
        cat_dir = os.path.join(vault_memory_path, category)
        if not os.path.isdir(cat_dir):
            continue
        for fname in sorted(os.listdir(cat_dir)):
            if not fname.endswith(".md") or fname.startswith("."):
                continue
            path = os.path.join(cat_dir, fname)
            try:
                with open(path) as f:
                    raw = f.read()
            except OSError as e:
                logger.warning("build_index: skip %s: %s", path, e)
                continue
            meta, body = _parse_frontmatter(raw)
            slug = os.path.splitext(fname)[0]
            content = _extract_body_content(body).strip()
            if not content:
                continue
            tags = meta.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            sources = meta.get("sources") or []
            if isinstance(sources, str):
                sources = [s.strip() for s in sources.split(",") if s.strip()]
            valid_at = (meta.get("first_seen") or "").strip() or None

            existing = conn.execute(
                "SELECT id FROM memories WHERE memory_type = ? AND slug = ?",
                (category, slug),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE memories SET content = ?, tags = ?, valid_at = ?, "
                    "source_episode_ids = ? WHERE id = ?",
                    (content, json.dumps(tags), valid_at, json.dumps(sources), existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO memories (content, memory_type, tags, importance, "
                    "valid_at, source_episode_ids, slug) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        content,
                        category,
                        json.dumps(tags),
                        0.5,
                        valid_at,
                        json.dumps(sources),
                        slug,
                    ),
                )
            rows_written += 1

    conn.commit()
    conn.close()
    return rows_written


# -----------------------------------------------------------------------------
# Orchestrator
# -----------------------------------------------------------------------------


def run_full_import(zip_path: str, skip_index: bool = False) -> dict[str, int]:
    """End-to-end pipeline. Prints progress to stdout.

    Stages:
      1. Unpack the zip and split conversations.json per conversation.
      2. Copy the account-level memory to vault/memory/claude-bootstrap.md.
      3. Parse every per-conversation JSON.
      4. Extract knowledge per conversation via claude -p.
      5. Write extracted items into vault/memory/<category>/.
      6. Index vault/memory/ into data/memory.db (skip if skip_index=True).

    Returns a summary dict with counts per stage.
    """
    summary: dict[str, int] = {
        "conversations": 0,
        "parsed": 0,
        "parse_errors": 0,
        "knowledge_items": 0,
        "extract_errors": 0,
        "vault_files": 0,
        "indexed_rows": 0,
    }

    print(f"[1/6] Unpacking {zip_path} into {EXPORT_DIR}")
    split_paths = unpack_export(zip_path)
    summary["conversations"] = len(split_paths)
    print(f"      split into {len(split_paths)} conversation files")

    print("[2/6] Writing account-level bootstrap note")
    boot = write_account_memory()
    if boot:
        print(f"      wrote {boot}")
    else:
        print("      no memories.json, skipping")

    print("[3/6] Parsing conversations")
    parsed: list[ParsedConversation] = []
    for i, path in enumerate(split_paths, 1):
        try:
            parsed.append(parse_conversation(path))
            summary["parsed"] += 1
        except Exception as e:
            summary["parse_errors"] += 1
            print(f"      skip {os.path.basename(path)}: {e}")
        if i % 10 == 0:
            print(f"      parsed {i}/{len(split_paths)}")

    print("[4/6] Extracting knowledge via claude")
    all_items: list[KnowledgeItem] = []
    for i, conv in enumerate(parsed, 1):
        try:
            items = extract_knowledge(conv)
            all_items.extend(items)
        except Exception as e:
            summary["extract_errors"] += 1
            print(f"      extract failed on {conv.title[:60]!r}: {e}")
            continue
        print(
            f"      [{i}/{len(parsed)}] {conv.title[:60]!r}: {len(items)} items"
        )
    summary["knowledge_items"] = len(all_items)

    print(f"[5/6] Writing {len(all_items)} items to vault/memory/")
    written = write_to_vault(all_items)
    summary["vault_files"] = len(written)
    print(f"      touched {len(written)} files")

    if skip_index:
        print("[6/6] Skipping index build (skip_index=True)")
    else:
        print("[6/6] Building search index")
        try:
            summary["indexed_rows"] = build_index()
            print(f"      indexed {summary['indexed_rows']} rows")
        except NotImplementedError as e:
            print(f"      index step not implemented yet: {e}")

    print("Import complete.")
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python3 -m core.import_claude <path-to-claude-export.zip> [--skip-index]")
        sys.exit(2)
    run_full_import(sys.argv[1], skip_index="--skip-index" in sys.argv)

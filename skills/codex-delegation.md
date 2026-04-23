---
name: codex-delegation
description: How Alfred development splits work between two coding agents on the Jetson. Claude Code (Ultra) handles judgment calls, architecture, and anything touching the voice pipeline or Alfred's personality. Codex CLI handles mechanical, well-scoped tasks. Includes the routing rules and the exact command shape for delegating from Claude Code to Codex.
---

# Codex Delegation

Alfred's development uses two coding agents in parallel. They live on the same Jetson, see the same filesystem, and run on existing subscriptions at zero API cost. The point is to keep Claude Code (Ultra) focused on the work that needs judgment, and farm out the rest to Codex.

## When to use which

### Claude Code (Ultra)

Reach for Claude Code when the task needs taste, cross-file reasoning, or familiarity with how Alfred's pieces fit together. Specifically:

- Architecture decisions. Anything that picks a new pattern, names a new abstraction, or changes how modules talk.
- Cross-file coordination. Edits that have to land in three places at once and stay consistent.
- Alfred personality or CLAUDE.md changes. The voice and the instructions are load-bearing; one wrong rewrite changes how Alfred talks for weeks.
- Voice pipeline work. Anything in `bridge/`, `core/voice.py`, `core/session.py`, or the audio path. Mistakes here surface as live failures while the user is mid-sentence.
- Session management. Resume windows, conversation summaries, vault writeback rules. Subtle, easy to break.
- Prompt engineering. Reflection prompts, extraction prompts, briefing prompts. Small wording changes have outsized effects.
- Complex integrations. New MCP servers, new external APIs, anything where the failure modes need exploring before code lands.

Default for anything requiring judgment.

### Codex CLI (`codex` on the Jetson)

Reach for Codex when the task is mechanical and you can describe it in one sentence. Specifically:

- Boilerplate. New utility scripts, new test scaffolds, new config files that follow an existing template.
- Bug fixes with a clear repro. Known input, known wrong output, known correct output.
- Tests. Unit tests for a function whose behavior is already specified.
- File format conversions. JSON to YAML, CSV to SQLite, that kind of thing.
- Documentation generation. README sections, function docstrings from existing code, changelog entries.
- Dependency updates. Bumping versions, regenerating lock files, fixing the easy breakage that follows.
- Throwaway one-shots. Quick scripts that exist for a single migration and then get deleted.

Best for well-scoped tasks you can describe in one sentence.

## How Claude Code delegates to Codex

When Claude Code recognizes a task as mechanical and well-scoped, it should stop, say "This is a good Codex task", and hand back the exact command. the user runs it in a separate terminal. The shape is:

```
codex "description of task"
```

Examples Claude Code should produce verbatim:

- `codex "fix the sync_state.py sprint field overwrite bug"`
- `codex "add a retry wrapper to reflect.py for claude rc=1 failures"`
- `codex "convert the contents of data/old_state.csv to data/old_state.sqlite with one row per record"`
- `codex "write pytest cases for core.import_claude._slugify covering ascii, unicode, empty, and >80 char inputs"`

The goal is one self-contained sentence that Codex can run on without follow-up. If the task needs three sentences of context, it is probably not a Codex task; keep it in Claude Code.

## How the user uses Codex directly

Same command, no Claude Code in the loop:

```
codex "fix the sync_state.py sprint field overwrite bug"
codex "add a retry wrapper to reflect.py for claude rc=1 failures"
```

Run from any directory inside `/mnt/nvme/alfred/`. Codex picks up the working tree the same way Claude Code does.

## Routing in practice

Quick test before delegating: if you can finish the sentence "the change is to ___" with a single concrete edit, it is a Codex task. If the sentence needs an "and" or a "but", keep it in Claude Code.

Cost of getting the routing wrong is low in one direction (Codex bounces back, Claude Code picks it up) and higher in the other (Claude Code burns a long context window on something Codex would have done in a minute). Bias toward Codex when in doubt on mechanical work, bias toward Claude Code when in doubt on anything touching Alfred's voice or state.

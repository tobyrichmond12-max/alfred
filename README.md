# Alfred

A voice-first personal AI assistant running on a Jetson Orin Nano 8GB.

Built across 27 development phases in 48 hours as a personal project.

## What Alfred does

- Voice assistant via iOS Action Button + AirPods
- Telegram bot as primary chat interface (@AlfredTheOS_bot)
- HUD display for Even Realities G2 smart glasses
- Calendar and task management (Google Calendar, Todoist)
- Email triage and financial awareness (Gmail)
- Academic integration (Canvas LMS)
- Screen awareness via laptop MCP server
- Obsidian vault as knowledge graph with semantic search
- Content ingestion from YouTube, Instagram, TikTok, X
- ADHD focus mode with pomodoro and context-switch detection
- Relationship CRM with contact tracking
- Nighttime journal and micro-journaling
- Commute optimization with Amtrak schedules
- Study buddy with Canvas-aware question generation
- Self-improving: ingests AI content and auto-implements relevant techniques

## Architecture

- Brain: Claude (Max plan) via claude -p on Jetson Orin Nano 8GB
- Delegation: Codex CLI handles mechanical coding, Claude reviews
- Voice: faster-whisper for transcription, Apple STT/TTS via iOS Shortcuts
- Memory: Obsidian vault with BM25 + semantic search (nomic-embed via Ollama)
- Interfaces: Telegram, iOS Shortcut (Action Button), G2 HUD, PWA dashboard
- Networking: Tailscale for secure device mesh
- Scheduling: 15+ cron jobs for reflections, journals, briefings, reviews

## Tech stack

Python, FastAPI, Claude Code, Codex CLI, Ollama, faster-whisper, Telegram Bot API, Google Calendar/Gmail API, Canvas API, Todoist API, Tailscale, systemd, SQLite, Obsidian

## Built by

Toby Richmond, Northeastern University

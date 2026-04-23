import type { HistoryResponse, MessageResponse, VoiceMemoResponse } from "./types";

export async function sendMessage(message: string): Promise<MessageResponse> {
  const resp = await fetch("/api/message", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  if (!resp.ok) {
    const detail = await resp.text();
    throw new Error(`Alfred backend returned ${resp.status}: ${detail}`);
  }
  return resp.json();
}

export async function fetchHistory(limit = 50): Promise<HistoryResponse> {
  const resp = await fetch(`/api/history?limit=${limit}`);
  if (!resp.ok) {
    throw new Error(`history fetch failed: ${resp.status}`);
  }
  return resp.json();
}

export async function sendVoiceMemo(
  audio: Blob,
  filename = "memo.webm",
): Promise<VoiceMemoResponse> {
  const form = new FormData();
  form.append("audio", audio, filename);
  const resp = await fetch("/api/voice-memo", {
    method: "POST",
    body: form,
  });
  if (!resp.ok) {
    let detail = "";
    try {
      const body = await resp.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      detail = await resp.text();
    }
    throw new Error(`voice memo failed (${resp.status}): ${detail}`);
  }
  return resp.json();
}

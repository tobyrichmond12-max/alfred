import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchHistory, sendMessage, sendVoiceMemo } from "./api";
import { currentPushState, pushSupported, subscribeToPush } from "./push";
import type { PushState } from "./push";
import type { Message } from "./types";

type Status =
  | "idle"
  | "loading-history"
  | "thinking"
  | "recording"
  | "transcribing"
  | "error";

function pickMimeType(): { mime: string; ext: string } {
  const candidates: Array<{ mime: string; ext: string }> = [
    { mime: "audio/webm;codecs=opus", ext: "webm" },
    { mime: "audio/webm", ext: "webm" },
    { mime: "audio/mp4;codecs=mp4a.40.2", ext: "m4a" },
    { mime: "audio/mp4", ext: "m4a" },
    { mime: "audio/ogg;codecs=opus", ext: "ogg" },
  ];
  const MR =
    typeof MediaRecorder !== "undefined" ? (MediaRecorder as typeof MediaRecorder) : null;
  if (!MR) return { mime: "", ext: "webm" };
  for (const c of candidates) {
    if (MR.isTypeSupported(c.mime)) return c;
  }
  return { mime: "", ext: "webm" };
}

function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  const mm = Math.floor(total / 60)
    .toString()
    .padStart(2, "0");
  const ss = (total % 60).toString().padStart(2, "0");
  return `${mm}:${ss}`;
}

export default function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [status, setStatus] = useState<Status>("loading-history");
  const [errorText, setErrorText] = useState<string | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [pushState, setPushState] = useState<PushState | null>(null);
  const [pushBusy, setPushBusy] = useState(false);

  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const recordStartRef = useRef<number>(0);
  const tickerRef = useRef<number | null>(null);
  const recordExtRef = useRef<string>("webm");

  useEffect(() => {
    fetchHistory(50)
      .then((data) => {
        setMessages(data.messages);
        setStatus("idle");
      })
      .catch(() => {
        setStatus("idle");
      });
  }, []);

  useEffect(() => {
    if (!pushSupported()) {
      setPushState("unsupported");
      return;
    }
    currentPushState().then(setPushState);
  }, []);

  async function handleEnablePush() {
    if (pushBusy) return;
    setPushBusy(true);
    try {
      const next = await subscribeToPush();
      setPushState(next);
    } catch (e) {
      setPushState("error");
      setErrorText(e instanceof Error ? e.message : String(e));
    } finally {
      setPushBusy(false);
    }
  }

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, status]);

  useEffect(() => {
    return () => {
      if (tickerRef.current !== null) window.clearInterval(tickerRef.current);
      streamRef.current?.getTracks().forEach((t) => t.stop());
    };
  }, []);

  const busy =
    status === "thinking" || status === "recording" || status === "transcribing";

  const statusLabel = useMemo(() => {
    if (status === "loading-history") return "loading";
    if (status === "thinking") return "thinking";
    if (status === "recording") return `recording ${formatElapsed(elapsedMs)}`;
    if (status === "transcribing") return "transcribing";
    if (status === "error") return "error";
    return "ready";
  }, [status, elapsedMs]);

  const appendMessage = useCallback((m: Message) => {
    setMessages((prev) => [...prev, m]);
  }, []);

  async function handleSend() {
    const text = input.trim();
    if (!text || busy) return;
    const now = new Date().toISOString();
    appendMessage({ role: "user", text, timestamp: now });
    setInput("");
    setStatus("thinking");
    setErrorText(null);
    try {
      const reply = await sendMessage(text);
      appendMessage({
        role: "alfred",
        text: reply.response,
        timestamp: reply.timestamp || new Date().toISOString(),
      });
      setStatus("idle");
    } catch (e) {
      setStatus("error");
      setErrorText(e instanceof Error ? e.message : String(e));
    } finally {
      inputRef.current?.focus();
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  async function startRecording() {
    if (busy) return;
    setErrorText(null);

    if (!navigator.mediaDevices || !window.MediaRecorder) {
      setStatus("error");
      setErrorText("This browser does not support audio recording.");
      return;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      setStatus("error");
      setErrorText(
        e instanceof Error && e.name === "NotAllowedError"
          ? "Microphone permission denied. Enable it in Safari settings, then reload."
          : `Microphone unavailable: ${e instanceof Error ? e.message : String(e)}`,
      );
      return;
    }

    streamRef.current = stream;
    const { mime, ext } = pickMimeType();
    recordExtRef.current = ext;

    let recorder: MediaRecorder;
    try {
      recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    } catch (e) {
      stream.getTracks().forEach((t) => t.stop());
      setStatus("error");
      setErrorText(`Could not start recorder: ${e instanceof Error ? e.message : String(e)}`);
      return;
    }

    chunksRef.current = [];
    recorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
    };
    recorder.onstop = () => {
      const type = mime || recorder.mimeType || "audio/webm";
      const blob = new Blob(chunksRef.current, { type });
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      if (tickerRef.current !== null) {
        window.clearInterval(tickerRef.current);
        tickerRef.current = null;
      }
      uploadMemo(blob);
    };

    recorderRef.current = recorder;
    recordStartRef.current = performance.now();
    setElapsedMs(0);
    tickerRef.current = window.setInterval(() => {
      setElapsedMs(performance.now() - recordStartRef.current);
    }, 250);
    recorder.start();
    setStatus("recording");
  }

  function stopRecording() {
    const r = recorderRef.current;
    if (r && r.state !== "inactive") {
      r.stop();
      setStatus("transcribing");
    }
  }

  async function uploadMemo(blob: Blob) {
    if (blob.size === 0) {
      setStatus("error");
      setErrorText("Empty recording. Try again.");
      return;
    }
    try {
      const reply = await sendVoiceMemo(blob, `memo.${recordExtRef.current}`);
      const now = reply.timestamp || new Date().toISOString();
      appendMessage({ role: "user", text: reply.transcription, timestamp: now });
      appendMessage({ role: "alfred", text: reply.response, timestamp: now });
      setStatus("idle");
    } catch (e) {
      setStatus("error");
      setErrorText(e instanceof Error ? e.message : String(e));
    }
  }

  function handleMicTap() {
    if (status === "recording") {
      stopRecording();
    } else {
      startRecording();
    }
  }

  const canSend = input.trim().length > 0 && !busy;
  const micDisabled = status === "thinking" || status === "transcribing";

  return (
    <div className="app">
      <header className="header">
        <div className="brand">A L F R E D</div>
        <div className={`status status-${status}`}>{statusLabel}</div>
        {pushState === "needs-permission" && (
          <button
            type="button"
            className="push-enable"
            onClick={handleEnablePush}
            disabled={pushBusy}
          >
            {pushBusy ? "Enabling..." : "Enable notifications"}
          </button>
        )}
        {pushState === "denied" && (
          <div className="push-hint">
            Notifications blocked. Enable them in Safari settings to get alerts.
          </div>
        )}
        {pushState === "unsupported" && (
          <div className="push-hint">
            Push not available here. On iOS, add Alfred to the home screen and open from there.
          </div>
        )}
        {pushState === "error" && (
          <div className="push-hint">
            Notification setup failed. Try again from the home screen.
          </div>
        )}
      </header>

      <div className="messages" ref={scrollRef}>
        {messages.length === 0 && status !== "loading-history" && (
          <div className="msg system">No history yet. Say something.</div>
        )}
        {messages.map((m, i) => (
          <div key={`${m.timestamp}-${i}`} className={`msg ${m.role}`}>
            {m.text}
          </div>
        ))}
        {(status === "thinking" || status === "transcribing") && (
          <div className="thinking-dots" aria-label="Alfred is thinking">
            <span />
            <span />
            <span />
          </div>
        )}
        {errorText && <div className="msg error">{errorText}</div>}
      </div>

      <div className="input-area">
        <button
          type="button"
          className={`mic-btn${status === "recording" ? " recording" : ""}`}
          onClick={handleMicTap}
          disabled={micDisabled}
          aria-label={status === "recording" ? "Stop recording" : "Record voice memo"}
          aria-pressed={status === "recording"}
        >
          {status === "recording" ? (
            <span className="rec-indicator">
              <span className="rec-dot" />
              {formatElapsed(elapsedMs)}
            </span>
          ) : (
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <rect x="9" y="3" width="6" height="11" rx="3" />
              <path d="M5 11a7 7 0 0 0 14 0" />
              <line x1="12" y1="18" x2="12" y2="22" />
            </svg>
          )}
        </button>
        <textarea
          ref={inputRef}
          className="msg-input"
          placeholder={
            status === "recording"
              ? "Recording..."
              : status === "transcribing"
                ? "Transcribing..."
                : "Message Alfred"
          }
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          autoComplete="off"
          autoCorrect="off"
          disabled={busy}
        />
        <button
          className="send-btn"
          onClick={handleSend}
          disabled={!canSend}
          aria-label="Send message"
        >
          &uarr;
        </button>
      </div>
    </div>
  );
}

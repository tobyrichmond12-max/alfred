export type Role = "user" | "alfred";

export interface Message {
  role: Role;
  text: string;
  timestamp: string;
}

export interface HistoryResponse {
  messages: Message[];
}

export interface MessageResponse {
  response: string;
  duration_ms: number;
  timestamp: string;
}

export interface VoiceMemoResponse {
  transcription: string;
  response: string;
  duration_ms: number;
  transcribe_ms: number;
  timestamp: string;
}

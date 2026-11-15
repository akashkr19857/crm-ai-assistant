export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8300";
export const WS_URL = API_URL.replace(/^http/, "ws");

export type User = { id: string; name: string; title: string; crm_role: string; team: string; role: string; workspaces: string[] };
export type Workspace = { id: string; name: string; description: string; documents: number };
export type Doc = {
  id: number; workspace_id: string; title: string; filename: string; source: string; status: string; progress: number;
  file_type: "pdf" | "docx"; pages: number; n_chunks: number; ocr_pages: number; tables: number; ingest_seconds: number; error: string; created_at: string;
};
export type Source = {
  chunk_id: string; doc_id: number; doc_title: string; workspace_id: string; page: number; section: string; kind: string;
  text: string; bm25_rank: number | null; vector_rank: number | null; fused_score: number; rerank_score: number | null; file_type?: "pdf" | "docx";
};
export type Citation = { n: number; chunk_id: string; doc_id: number; doc_title: string; page: number; section: string };
export type Step = { node: string; detail: string; ms: number; pages?: string[] };
export type ChatResult = {
  question: string; answer: string; not_found: boolean; gate: string; citations: Citation[]; sources: Source[];
  latency_ms: number; retrieval_ms: number; rerank_ms: number; llm_ms: number; prompt_tokens: number;
  completion_tokens: number; steps: Step[]; trace_id: number | null;
};
export type TraceChunk = {
  chunk_id: string; doc_title: string; page: number; rerank_score: number | null; bm25_rank: number | null; vector_rank: number | null;
};
export type Trace = {
  id: number; user_id: string; workspaces: string[]; mode: string; question: string; answer: string; not_found: boolean;
  latency_ms: number; retrieval_ms: number; rerank_ms: number; llm_ms: number; prompt_tokens: number; completion_tokens: number;
  chunks: TraceChunk[]; steps: Step[]; created_at: string;
};

const TOKEN_KEY = "crm_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(t: string | null) {
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not json */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export function pageImageUrl(docId: number, page: number, chunkId?: string) {
  const q = new URLSearchParams({ token: getToken() ?? "" });
  if (chunkId) q.set("chunk_id", chunkId);
  return `${API_URL}/api/documents/${docId}/pages/${page}.png?${q.toString()}`;
}

export function eventsSocket(onEvent: (ev: Record<string, unknown>) => void): () => void {
  const token = getToken();
  if (!token) return () => {};
  let ws: WebSocket | null = null;
  let closed = false;
  let retry: ReturnType<typeof setTimeout> | undefined;
  const connect = () => {
    ws = new WebSocket(`${WS_URL}/ws/events?token=${encodeURIComponent(token)}`);
    ws.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data));
      } catch {
        /* ignore */
      }
    };
    ws.onclose = () => {
      if (!closed) retry = setTimeout(connect, 2000);
    };
  };
  connect();
  return () => {
    closed = true;
    if (retry) clearTimeout(retry);
    ws?.close();
  };
}

// ---------------------------------------------------------------- assistant
export type ToolCall = { tool: string; args: Record<string, unknown>; ok: boolean; ms: number; error?: string; kind?: string; candidates?: string[]; result?: Record<string, unknown> };
export type KnowledgeSource = { n: number; doc_id: number; doc_title: string; page: number; chunk_id: string; text: string };
export type PendingAction = { action: string; args: Record<string, unknown>; summary: string; status?: string };
export type Validation = { checked?: boolean; passed?: boolean; issues?: string[]; first_issues?: string[]; regenerated?: boolean; fallback?: boolean; not_found?: boolean };
export type AssistantStep = { node: string; detail: string; ms: number; intent?: string; query?: string };
export type Message = {
  id: number; conversation_id: number; role: "user" | "assistant"; content: string; intent: string; route: string; standalone_query: string;
  tool_calls: ToolCall[]; sources: KnowledgeSource[]; pending_action: PendingAction | Record<string, never>; validation: Validation;
  latency_ms: number; llm_calls: number; prompt_tokens: number; completion_tokens: number; steps: AssistantStep[]; created_at: string;
};
export type Conversation = { id: number; title: string; memory: Record<string, string>; created_at: string };

export function fileUrl(path: string) {
  const sep = path.includes("?") ? "&" : "?";
  return `${API_URL}${path}${sep}token=${encodeURIComponent(getToken() ?? "")}`;
}

"use client";

import * as React from "react";
import {
  AlertTriangle, Bot, Check, CheckCircle2, ChevronRight, Database, Loader2, MessageSquarePlus, Send, ShieldAlert, Sparkles, Wrench, X,
} from "lucide-react";
import { api, getToken, WS_URL, type AssistantStep, type Conversation, type KnowledgeSource, type Message, type PendingAction } from "@/lib/api";
import { useAuth } from "@/components/auth";
import { AnswerText } from "@/components/answer-text";
import { SourceViewer, type SourceRef } from "@/components/source-viewer";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/input";
import { cn, ms } from "@/lib/utils";

const INTENT_META: Record<string, { label: string; variant: "default" | "success" | "warning" | "secondary" | "destructive" }> = {
  crm_data: { label: "CRM data", variant: "default" },
  knowledge: { label: "Knowledge", variant: "success" },
  workflow: { label: "Workflow", variant: "warning" },
  chitchat: { label: "Chit-chat", variant: "secondary" },
  out_of_scope: { label: "Out of scope", variant: "destructive" },
};

const SUGGESTIONS: Record<string, string[]> = {
  sales_rep: ["What is my open pipeline by stage?", "Which of my accounts renew in the next 60 days?", "Tell me about Cedar Energy",
    "Who has to approve a 15% discount?"],
  sales_manager: ["Show the team pipeline by owner", "Which accounts are at risk?", "List deals closing in the next 30 days over $100,000",
    "What must happen before a deal moves to Negotiation?"],
  support_agent: ["How many open P1 or P2 tickets are there?", "Which tickets have breached their SLA?",
    "How do I fix an SSO login loop after a password reset?", "Show me the pipeline"],
  admin: ["Show the pipeline by owner", "Which accounts are at risk?", "How many open P1 or P2 tickets are there?", "What are the API rate limits?"],
};

function IntentBadge({ intent }: { intent: string }) {
  const m = INTENT_META[intent];
  if (!m) return null;
  return <Badge variant={m.variant} data-testid="intent-badge">{m.label}</Badge>;
}

export default function AssistantPage() {
  const { user } = useAuth();
  const [convs, setConvs] = React.useState<Conversation[]>([]);
  const [convId, setConvId] = React.useState<number | null>(null);
  const [messages, setMessages] = React.useState<Message[]>([]);
  const [liveSteps, setLiveSteps] = React.useState<AssistantStep[]>([]);
  const [pendingQ, setPendingQ] = React.useState<string | null>(null);
  const [input, setInput] = React.useState("");
  const [selected, setSelected] = React.useState<Message | null>(null);
  const [source, setSource] = React.useState<SourceRef | null>(null);
  const [error, setError] = React.useState("");
  const wsRef = React.useRef<WebSocket | null>(null);
  const bottomRef = React.useRef<HTMLDivElement>(null);
  const busy = pendingQ !== null;

  const loadConvs = React.useCallback(() => api<Conversation[]>("/api/conversations").then(setConvs).catch((e) => setError(e.message)), []);
  React.useEffect(() => {
    loadConvs();
    return () => wsRef.current?.close();
  }, [loadConvs]);
  React.useEffect(() => {
    if (convId === null) return setMessages([]);
    api<Message[]>(`/api/conversations/${convId}/messages`).then((m) => {
      setMessages(m);
      setSelected([...m].reverse().find((x) => x.role === "assistant") ?? null);
    });
  }, [convId]);
  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, liveSteps, pendingQ]);

  const socket = (): Promise<WebSocket> =>
    new Promise((resolve, reject) => {
      const cur = wsRef.current;
      if (cur && cur.readyState === WebSocket.OPEN) return resolve(cur);
      const ws = new WebSocket(`${WS_URL}/ws/assistant?token=${encodeURIComponent(getToken() ?? "")}`);
      ws.onopen = () => resolve(ws);
      ws.onerror = () => reject(new Error("WebSocket connection failed - is the API running on :8300?"));
      wsRef.current = ws;
    });

  const send = async (text?: string) => {
    const content = (text ?? input).trim();
    if (!content || busy) return;
    setInput("");
    setError("");
    let id = convId;
    if (id === null) {
      const c = await api<Conversation>("/api/conversations", { method: "POST" });
      id = c.id;
      setConvId(c.id);
      setConvs((cs) => [{ ...c, title: content.slice(0, 80) }, ...cs]);
    }
    setPendingQ(content);
    setLiveSteps([]);
    try {
      const ws = await socket();
      ws.onmessage = (m) => {
        const ev = JSON.parse(m.data);
        if (ev.type === "step") setLiveSteps((s) => [...s, ev.step]);
        else if (ev.type === "final") {
          const msg: Message = ev.message;
          api<Message[]>(`/api/conversations/${msg.conversation_id}/messages`).then((all) => {
            setMessages(all);
            setSelected(all[all.length - 1]);
            setPendingQ(null);
            setLiveSteps([]);
          });
          const first = msg.sources?.[0];
          if (first) setSource({ docId: first.doc_id, page: first.page, chunkId: first.chunk_id, title: first.doc_title, text: first.text });
        } else if (ev.type === "error") {
          setError(String(ev.error));
          setPendingQ(null);
        }
      };
      ws.send(JSON.stringify({ conversation_id: id, content }));
    } catch (e) {
      setError((e as Error).message);
      setPendingQ(null);
    }
  };

  const decide = async (m: Message, accept: boolean) => {
    try {
      await api<Message>(`/api/messages/${m.id}/confirm`, { method: "POST", body: JSON.stringify({ accept }) });
      const all = await api<Message[]>(`/api/conversations/${m.conversation_id}/messages`);
      setMessages(all);
      setSelected(all[all.length - 1]);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const newChat = () => {
    setConvId(null);
    setMessages([]);
    setSelected(null);
    setSource(null);
  };
  const role = user?.crm_role ?? "sales_rep";
  const memory = convs.find((c) => c.id === convId)?.memory;

  return (
    <div className="grid h-[calc(100vh-7.5rem)] min-h-[600px] grid-cols-1 gap-4 lg:grid-cols-[220px_minmax(0,1fr)_minmax(320px,38%)]">
      <Card className="hidden min-h-0 flex-col lg:flex">
        <div className="p-3"><Button className="w-full" variant="outline" size="sm" onClick={newChat} data-testid="new-chat"><MessageSquarePlus className="h-4 w-4" />New chat</Button></div>
        <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-2 pb-3">
          {convs.map((c) => (
            <button key={c.id} onClick={() => setConvId(c.id)} className={cn("w-full truncate rounded-md px-2 py-1.5 text-left text-sm", c.id === convId ? "bg-primary/12 font-medium text-primary" : "text-muted-foreground hover:bg-accent")}>
              {c.title}
            </button>
          ))}
        </div>
      </Card>

      <Card className="flex min-h-0 flex-col">
        <div className="flex-1 space-y-4 overflow-y-auto p-4" data-testid="thread">
          {!messages.length && !busy && (
            <div className="mx-auto max-w-lg space-y-4 pt-8 text-center">
              <Bot className="mx-auto h-10 w-10 text-primary" />
              <div>
                <h1 className="text-lg font-semibold">Ask anything about your CRM</h1>
                <p className="text-sm text-muted-foreground">Accounts, deals, pipeline, renewals and tickets you are allowed to see, product help and sales policies, or ask me to create tasks and update deals.</p>
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                {(SUGGESTIONS[role] ?? SUGGESTIONS.admin).map((s) => (
                  <Button key={s} variant="outline" className="h-auto whitespace-normal py-2 text-left text-xs" onClick={() => send(s)} data-testid="suggestion">{s}</Button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m) => m.role === "user" ? (
            <div key={m.id} className="ml-auto w-fit max-w-[85%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground" data-testid="user-message">{m.content}</div>
          ) : (
            <div key={m.id} onClick={() => setSelected(m)} data-testid="assistant-message"
              className={cn("cursor-pointer space-y-2 rounded-lg border p-3 transition-colors", selected?.id === m.id ? "border-primary/60 bg-primary/5" : "hover:bg-accent/40")}>
              <div className="flex flex-wrap items-center gap-1.5">
                <IntentBadge intent={m.intent} />
                {m.tool_calls.map((t, i) => (
                  <Badge key={i} variant={t.ok ? "outline" : "destructive"}>{t.ok ? <Wrench className="h-3 w-3" /> : <ShieldAlert className="h-3 w-3" />}{t.tool}</Badge>
                ))}
                {m.validation?.checked && m.intent === "crm_data" && (
                  <Badge variant={m.validation.regenerated ? "warning" : "success"} data-testid="validation-badge">
                    <CheckCircle2 className="h-3 w-3" />{m.validation.fallback ? "validated (fallback)" : m.validation.regenerated ? "corrected" : "validated"}
                  </Badge>
                )}
                {m.latency_ms > 0 && <span className="ml-auto text-[11px] text-muted-foreground">{ms(m.latency_ms)}</span>}
              </div>
              <AnswerText text={m.content} citations={m.sources.map((s) => ({ ...s, section: "" }))} onCite={(n) => {
                const s = m.sources.find((x) => x.n === n);
                if (s) setSource({ docId: s.doc_id, page: s.page, chunkId: s.chunk_id, title: s.doc_title, text: s.text });
                setSelected(m);
              }} />
              {"action" in (m.pending_action ?? {}) && <ActionCard m={m} action={m.pending_action as PendingAction} onDecide={decide} />}
            </div>
          ))}
          {busy && (
            <div className="space-y-2">
              <div className="ml-auto w-fit max-w-[85%] rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground">{pendingQ}</div>
              <div className="space-y-1 rounded-lg border p-3 text-xs text-muted-foreground" data-testid="live-steps">
                <div className="flex items-center gap-2"><Loader2 className="h-3.5 w-3.5 animate-spin text-primary" />Thinking…</div>
                {liveSteps.map((s, i) => <div key={i} className="flex items-center gap-1"><ChevronRight className="h-3 w-3" /><b className="font-medium text-foreground">{s.node}</b> {s.detail}</div>)}
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>
        {error && <p className="px-4 text-sm text-destructive">{error}</p>}
        {memory && Object.keys(memory).length > 0 && (
          <div className="flex flex-wrap items-center gap-1 border-t px-4 pt-2 text-[11px] text-muted-foreground" data-testid="memory">
            In focus: {Object.entries(memory).map(([k, v]) => <Badge key={k} variant="secondary">{k}: {String(v)}</Badge>)}
          </div>
        )}
        <div className="flex gap-2 p-3">
          <Textarea rows={2} value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask about your accounts, deals, tickets, product or policies…" data-testid="chat-input"
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
          <Button onClick={() => send()} disabled={busy || !input.trim()} aria-label="Send" data-testid="send"><Send className="h-4 w-4" /></Button>
        </div>
      </Card>

      <Inspector m={selected} source={source} onSource={setSource} />
    </div>
  );
}

function ActionCard({ m, action, onDecide }: { m: Message; action: PendingAction; onDecide: (m: Message, accept: boolean) => void }) {
  const done = action.status;
  return (
    <div className="space-y-2 rounded-md border border-warning/40 bg-warning/10 p-3" data-testid="action-card" onClick={(e) => e.stopPropagation()}>
      <div className="flex items-center gap-2 text-sm font-medium"><AlertTriangle className="h-4 w-4 text-warning" />Confirm CRM change</div>
      <p className="text-sm">{action.summary}</p>
      {done ? (
        <Badge variant={done === "done" ? "success" : "secondary"}>{done}</Badge>
      ) : (
        <div className="flex gap-2">
          <Button size="sm" onClick={() => onDecide(m, true)} data-testid="confirm-action"><Check className="h-4 w-4" />Confirm</Button>
          <Button size="sm" variant="outline" onClick={() => onDecide(m, false)} data-testid="cancel-action"><X className="h-4 w-4" />Cancel</Button>
        </div>
      )}
    </div>
  );
}

function Inspector({ m, source, onSource }: { m: Message | null; source: SourceRef | null; onSource: (s: SourceRef) => void }) {
  return (
    <Card className="flex min-h-0 flex-col">
      <CardHeader className="pb-2"><CardTitle className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-primary" />How this answer was made</CardTitle></CardHeader>
      <CardContent className="min-h-0 flex-1 space-y-4 overflow-y-auto text-sm" data-testid="inspector">
        {!m ? <p className="text-muted-foreground">Ask a question, then click an answer to see its intent, the CRM tool calls, validation and sources.</p> : (
          <>
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="rounded-md bg-muted/60 p-2"><div className="text-muted-foreground">Intent</div><div className="font-medium">{INTENT_META[m.intent]?.label ?? m.intent ?? "–"}</div></div>
              <div className="rounded-md bg-muted/60 p-2"><div className="text-muted-foreground">Latency · LLM calls</div><div className="font-medium">{ms(m.latency_ms)} · {m.llm_calls}</div></div>
            </div>
            {m.standalone_query && (
              <div><div className="text-xs text-muted-foreground">Standalone query (multi-turn context resolved)</div><div className="rounded-md border bg-card p-2 text-xs" data-testid="standalone-query">{m.standalone_query}</div></div>
            )}
            {m.steps.length > 0 && (
              <div><div className="mb-1 text-xs text-muted-foreground">Steps</div>
                <ol className="space-y-1 text-xs">{m.steps.map((s, i) => <li key={i} className="flex gap-2"><span className="w-20 shrink-0 font-medium">{s.node}</span><span className="min-w-0 flex-1 break-words text-muted-foreground">{s.detail}</span><span className="tabular-nums text-muted-foreground">{ms(s.ms)}</span></li>)}</ol>
              </div>
            )}
            {m.tool_calls.map((t, i) => (
              <div key={i} className="space-y-1 rounded-md border p-2" data-testid="tool-call">
                <div className="flex items-center gap-2 text-xs font-medium"><Database className="h-3.5 w-3.5 text-primary" />{t.tool}<span className="text-muted-foreground">{ms(t.ms)}</span>{!t.ok && <Badge variant="destructive">{t.kind}</Badge>}</div>
                <pre className="overflow-x-auto rounded bg-muted/60 p-1.5 text-[11px]">{JSON.stringify(t.args)}</pre>
                {t.ok ? <ResultPreview result={t.result ?? {}} /> : <p className="text-xs text-destructive">{t.error}</p>}
              </div>
            ))}
            {m.validation?.checked && (
              <div className="rounded-md border p-2 text-xs" data-testid="validation">
                <div className="font-medium">Response validation</div>
                {m.intent === "crm_data" ? (
                  <p className="text-muted-foreground">
                    Every number and date in the answer was checked against the tool results.{" "}
                    {m.validation.first_issues?.length ? <>First draft failed ({m.validation.first_issues.join("; ")}), so it was {m.validation.fallback ? "replaced with a data-only answer" : "regenerated"}.</> : "First draft passed."}
                  </p>
                ) : <p className="text-muted-foreground">{m.validation.not_found ? "Documents don't cover this: answered 'not found' instead of guessing." : "Answer cites the documentation it uses."}</p>}
              </div>
            )}
            {m.sources.length > 0 && (
              <div className="space-y-1"><div className="text-xs text-muted-foreground">Sources</div>
                {m.sources.map((s: KnowledgeSource) => (
                  <button key={s.chunk_id} onClick={() => onSource({ docId: s.doc_id, page: s.page, chunkId: s.chunk_id, title: s.doc_title, text: s.text })}
                    className="block w-full rounded-md border p-2 text-left text-xs hover:bg-accent">[{s.n}] {s.doc_title} · p. {s.page}</button>
                ))}
              </div>
            )}
            {source && <div className="h-[420px]"><SourceViewer source={source} /></div>}
          </>
        )}
      </CardContent>
    </Card>
  );
}

function ResultPreview({ result }: { result: Record<string, unknown> }) {
  const scalars = Object.entries(result).filter(([, v]) => typeof v !== "object" || v === null);
  const lists = Object.entries(result).filter(([, v]) => Array.isArray(v)) as [string, Record<string, unknown>[]][];
  const objects = Object.entries(result).filter(([, v]) => v && typeof v === "object" && !Array.isArray(v)) as [string, Record<string, unknown>][];
  const fmt = (v: unknown) => (typeof v === "number" && v > 999 ? v.toLocaleString() : String(v));
  return (
    <div className="space-y-1 text-[11px]">
      {scalars.length > 0 && <div className="flex flex-wrap gap-1">{scalars.map(([k, v]) => <Badge key={k} variant="secondary">{k}: {fmt(v)}</Badge>)}</div>}
      {objects.map(([k, o]) => <div key={k} className="text-muted-foreground"><b>{k}</b>: {Object.entries(o).map(([a, b]) => `${a}=${fmt(b)}`).join(" · ")}</div>)}
      {lists.map(([k, rows]) => rows.length > 0 && (
        <div key={k} className="overflow-x-auto">
          <div className="font-medium">{k} ({rows.length} shown)</div>
          <table className="w-full"><tbody>
            {rows.slice(0, 6).map((r, i) => (
              <tr key={i} className="border-t">{Object.entries(r).filter(([c]) => !["id"].includes(c)).slice(0, 5).map(([c, v]) => <td key={c} className="py-0.5 pr-2">{fmt(v)}</td>)}</tr>
            ))}
          </tbody></table>
        </div>
      ))}
    </div>
  );
}

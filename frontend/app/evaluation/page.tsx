"use client";

import * as React from "react";
import { CheckCircle2, Cpu, XCircle } from "lucide-react";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { ms, pct } from "@/lib/utils";

type Acc = { accuracy: number | null; n: number };
type M = {
  intent_accuracy: number; intent_n: number; tool_selection_accuracy: number; crm_answer_accuracy: Acc; multi_turn_accuracy: Acc;
  knowledge_accuracy: Acc; rbac: Acc & { leaks: number }; workflow_accuracy: Acc; scope_accuracy: Acc;
  crm_answers_with_unsupported_values: number; crm_first_drafts_failed_validation: number; latency_p50_ms: number; llm_calls_avg: number; n: number;
  latency_ms: Record<string, { p50: number; max: number }>;
};
type Metrics = { with_validation: M; without_validation?: { crm_answer_accuracy: number; crm_answers_with_unsupported_values: number; n: number } };
type Run = { id: number; release: string; label: string; metrics: Metrics; hardware: string; created_at: string };
type Row = { kind: string; user: string; q: string; answer: string; intent: string; expected_intent: string; ok: boolean; ms: number; llm_calls: number; standalone?: string; unsupported_values?: string[] };

function Metric({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg border bg-card p-3" data-testid="eval-metric">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      {sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

const KIND_LABEL: Record<string, string> = { crm: "CRM data", multi: "Multi-turn", knowledge: "Knowledge", rbac: "Access control", workflow: "Workflow", scope: "Chit-chat / out of scope" };

export default function EvaluationPage() {
  const [runs, setRuns] = React.useState<Run[]>([]);
  const [runId, setRunId] = React.useState<number | null>(null);
  const [rows, setRows] = React.useState<Row[]>([]);
  React.useEffect(() => {
    api<Run[]>("/api/eval/runs").then((r) => {
      setRuns(r);
      if (r.length) setRunId(r[r.length - 1].id);
    });
  }, []);
  React.useEffect(() => {
    if (runId) api<{ details: { rows: Row[] } }>(`/api/eval/runs/${runId}`).then((r) => setRows(r.details.rows ?? []));
  }, [runId]);
  const run = runs.find((r) => r.id === runId);
  if (!runs.length) return <p className="text-sm text-muted-foreground">No evaluation runs yet. Run <code>python backend/eval/run_eval.py</code>.</p>;
  if (!run) return null;
  const m = run.metrics.with_validation;
  const off = run.metrics.without_validation;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Evaluation</h1>
          <p className="text-sm text-muted-foreground">{m.n} scripted conversations run end to end with the local LLM. Expected CRM values come from direct SQL on the database.</p>
        </div>
        <Select value={runId ?? undefined} onChange={(e) => setRunId(Number(e.target.value))} aria-label="Evaluation run">
          {runs.map((r) => <option key={r.id} value={r.id}>{r.release} · {r.label} · {new Date(r.created_at).toLocaleString()}</option>)}
        </Select>
      </div>
      <p className="flex items-center gap-2 text-xs text-muted-foreground"><Cpu className="h-3.5 w-3.5" />{run.hardware}</p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Metric label="Intent detection" value={pct(m.intent_accuracy, 0)} sub={`${m.intent_n} messages, 5 intents`} />
        <Metric label="Tool selection" value={pct(m.tool_selection_accuracy, 0)} sub="right CRM tool for data questions" />
        <Metric label="CRM answer accuracy" value={pct(m.crm_answer_accuracy.accuracy, 0)} sub={`${m.crm_answer_accuracy.n} questions vs SQL ground truth`} />
        <Metric label="Multi-turn follow-ups" value={pct(m.multi_turn_accuracy.accuracy, 0)} sub={`${m.multi_turn_accuracy.n} follow-up turns`} />
        <Metric label="Knowledge answers" value={pct(m.knowledge_accuracy.accuracy, 0)} sub={`${m.knowledge_accuracy.n} incl. 1 unanswerable`} />
        <Metric label="Access-control leaks" value={String(m.rbac.leaks)} sub={`${m.rbac.n} attempts to read forbidden data`} />
        <Metric label="Workflow actions" value={pct(m.workflow_accuracy.accuracy, 0)} sub="right action + target, awaiting confirmation" />
        <Metric label="Latency (median)" value={ms(m.latency_p50_ms)} sub={`${m.llm_calls_avg} LLM calls per answer`} />
      </div>
      {off && (
        <Card>
          <CardHeader><CardTitle>Response validation: does it reduce hallucinations?</CardTitle><CardDescription>Same CRM questions, validation on vs off. &quot;Unsupported values&quot; = numbers or dates in the final answer that are not in the CRM data.</CardDescription></CardHeader>
          <CardContent className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted-foreground"><tr><th className="py-1 pr-3" /><th className="pr-3">Answers with unsupported values</th><th>Answer accuracy</th></tr></thead>
              <tbody>
                <tr className="border-t"><td className="py-1.5 pr-3">Without validation</td><td className="pr-3 tabular-nums">{off.crm_answers_with_unsupported_values} / {off.n}</td><td className="tabular-nums">{pct(off.crm_answer_accuracy, 0)}</td></tr>
                <tr className="border-t font-medium"><td className="py-1.5 pr-3">With validation</td><td className="pr-3 tabular-nums">{m.crm_answers_with_unsupported_values} / {m.crm_answer_accuracy.n}</td><td className="tabular-nums">{pct(m.crm_answer_accuracy.accuracy, 0)}</td></tr>
              </tbody>
            </table>
            <p className="mt-2 text-xs text-muted-foreground">{m.crm_first_drafts_failed_validation} first drafts failed validation and were regenerated or replaced with a data-only answer.</p>
          </CardContent>
        </Card>
      )}
      <Card>
        <CardHeader><CardTitle>Every case</CardTitle></CardHeader>
        <CardContent className="overflow-x-auto p-0">
          <table className="w-full text-xs" data-testid="eval-cases">
            <thead className="bg-muted/60 text-left text-muted-foreground"><tr><th className="p-2 pl-4" /><th className="p-2">Type</th><th className="p-2">User</th><th className="p-2">Question</th><th className="p-2">Answer</th><th className="p-2">Intent</th><th className="p-2">Time</th></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i} className="border-t align-top">
                  <td className="p-2 pl-4">{r.ok ? <CheckCircle2 className="h-4 w-4 text-success" /> : <XCircle className="h-4 w-4 text-destructive" />}</td>
                  <td className="p-2"><Badge variant="secondary">{KIND_LABEL[r.kind] ?? r.kind}</Badge></td>
                  <td className="p-2">{r.user}</td>
                  <td className="max-w-xs p-2">{r.q}{r.standalone && r.standalone !== r.q && <div className="text-muted-foreground">→ {r.standalone}</div>}</td>
                  <td className="max-w-md p-2 text-muted-foreground">{r.answer.slice(0, 280)}{r.answer.length > 280 ? "…" : ""}</td>
                  <td className="p-2">{r.intent}{r.intent !== r.expected_intent && <div className="text-destructive">expected {r.expected_intent}</div>}</td>
                  <td className="p-2 tabular-nums">{ms(r.ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}

"use client";

import * as React from "react";
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ms } from "@/lib/utils";

type Analytics = {
  messages: number; p50_ms: number; p95_ms: number; intents: { intent: string; count: number; p50_ms: number }[];
  tools: { tool: string; count: number }[]; access_denied: number; llm_calls_avg: number;
  validation: { validated: number; failed_first_draft: number; regenerated: number; fallback: number };
};

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return <div className="rounded-lg border bg-card p-3"><div className="text-xs text-muted-foreground">{label}</div><div className="text-2xl font-semibold tabular-nums">{value}</div>{sub && <div className="text-[11px] text-muted-foreground">{sub}</div>}</div>;
}

export default function AnalyticsPage() {
  const [a, setA] = React.useState<Analytics | null>(null);
  React.useEffect(() => {
    api<Analytics>("/api/analytics").then(setA);
  }, []);
  if (!a) return null;
  const v = a.validation;
  return (
    <div className="space-y-4">
      <div><h1 className="text-xl font-semibold">Assistant analytics</h1><p className="text-sm text-muted-foreground">Live usage from stored conversations (admins see all users).</p></div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Assistant answers" value={String(a.messages)} sub={`${a.llm_calls_avg} LLM calls on average`} />
        <Stat label="Latency" value={ms(a.p50_ms)} sub={`p95 ${ms(a.p95_ms)}`} />
        <Stat label="Data answers validated" value={String(v.validated)} sub={`${v.failed_first_draft} first drafts failed · ${v.regenerated} regenerated · ${v.fallback} fallback`} />
        <Stat label="Access-denied tool calls" value={String(a.access_denied)} sub="blocked by role-based access" />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card><CardHeader><CardTitle>Intents</CardTitle><CardDescription>Messages per detected intent</CardDescription></CardHeader>
          <CardContent className="h-64"><ResponsiveContainer><BarChart data={a.intents}><XAxis dataKey="intent" tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }} /><YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }} /><Tooltip cursor={{ fill: "hsl(var(--muted))" }} /><Bar dataKey="count" fill="hsl(var(--primary))" radius={[4, 4, 0, 0]} /></BarChart></ResponsiveContainer></CardContent></Card>
        <Card><CardHeader><CardTitle>Tool usage</CardTitle><CardDescription>CRM tools chosen by the planner</CardDescription></CardHeader>
          <CardContent className="h-64"><ResponsiveContainer><BarChart data={a.tools} layout="vertical" margin={{ left: 20 }}><XAxis type="number" allowDecimals={false} hide /><YAxis type="category" dataKey="tool" width={120} interval={0} tick={{ fontSize: 11, fill: "hsl(var(--muted-foreground))" }} /><Tooltip cursor={{ fill: "hsl(var(--muted))" }} /><Bar dataKey="count" fill="hsl(var(--success))" radius={[0, 4, 4, 0]} /></BarChart></ResponsiveContainer></CardContent></Card>
      </div>
      <Card><CardHeader><CardTitle>Latency by intent</CardTitle></CardHeader>
        <CardContent className="flex flex-wrap gap-4 text-sm">{a.intents.map((i) => <div key={i.intent}><span className="text-muted-foreground">{i.intent}</span> <b className="tabular-nums">{ms(i.p50_ms)}</b></div>)}</CardContent></Card>
    </div>
  );
}

"use client";

import * as React from "react";
import { Lock } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/components/auth";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type Row = Record<string, unknown>;
type Tab = "accounts" | "pipeline" | "tickets" | "tasks" | "audit";
const TABS: { id: Tab; label: string }[] = [
  { id: "accounts", label: "Accounts" }, { id: "pipeline", label: "Pipeline" }, { id: "tickets", label: "Tickets" },
  { id: "tasks", label: "Open tasks" }, { id: "audit", label: "Assistant audit log" },
];
const STAGES = ["Prospecting", "Qualification", "Proposal", "Negotiation"];
const money = (v: unknown) => (typeof v === "number" ? `$${v.toLocaleString()}` : "–");

export default function CrmPage() {
  const { user } = useAuth();
  const [tab, setTab] = React.useState<Tab>("accounts");
  const [data, setData] = React.useState<Row | Row[] | null>(null);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    let current = true; // ignore responses for a tab the user already left
    setData(null);
    setError("");
    const path = { accounts: "/api/crm/accounts", pipeline: "/api/crm/deals", tickets: "/api/crm/tickets", tasks: "/api/crm/activities?open_tasks_only=true", audit: "/api/crm/audit" }[tab];
    api<Row | Row[]>(path).then((d) => current && setData(d)).catch((e) => current && setError(e.message));
    return () => {
      current = false;
    };
  }, [tab]);

  const rows = (key: string) => ((data as Row)?.[key] as Row[]) ?? [];
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">CRM data</h1>
        <p className="text-sm text-muted-foreground">Exactly what <b>{user?.name}</b> ({user?.crm_role.replace("_", " ")}) is allowed to see. The assistant uses the same access-controlled API, so it can never reveal more.</p>
      </div>
      <div className="flex gap-1 overflow-x-auto border-b" role="tablist">
        {TABS.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)} data-testid={`crm-tab-${t.id}`}
            className={cn("whitespace-nowrap border-b-2 px-3 py-2 text-sm", tab === t.id ? "border-primary font-medium text-primary" : "border-transparent text-muted-foreground hover:text-foreground")}>{t.label}</button>
        ))}
      </div>
      {error && (
        <Card><CardContent className="flex items-center gap-2 pt-4 text-sm"><Lock className="h-4 w-4 text-destructive" /><span data-testid="crm-denied">{error.replace(/^403: /, "")}</span></CardContent></Card>
      )}
      {!error && data && tab === "accounts" && (
        <Card><CardHeader><CardTitle>{(data as Row).count as number} accounts</CardTitle>{"total_arr" in (data as Row) && <CardDescription>Total ARR {money((data as Row).total_arr)}</CardDescription>}</CardHeader>
          <CardContent className="overflow-x-auto p-0">
            <table className="w-full text-sm" data-testid="accounts-table"><thead className="bg-muted/60 text-left text-xs text-muted-foreground"><tr><th className="p-2 pl-4">Account</th><th className="p-2">Industry</th><th className="p-2">Tier</th><th className="p-2">Region</th><th className="p-2">Owner</th><th className="p-2 text-right">ARR</th><th className="p-2 text-right">Health</th><th className="p-2">Renewal</th></tr></thead>
              <tbody>{rows("accounts").map((a) => (
                <tr key={a.id as number} className="border-t"><td className="p-2 pl-4 font-medium">{a.name as string}</td><td className="p-2">{a.industry as string}</td><td className="p-2">{a.tier as string}</td><td className="p-2">{a.region as string}</td><td className="p-2">{a.owner as string}</td>
                  <td className="p-2 text-right tabular-nums">{"arr" in a ? money(a.arr) : <Lock className="ml-auto h-3 w-3 text-muted-foreground" />}</td>
                  <td className="p-2 text-right"><Badge variant={(a.health_score as number) < 50 ? "destructive" : (a.health_score as number) < 75 ? "warning" : "success"}>{a.health_score as number}</Badge></td><td className="p-2 tabular-nums">{a.renewal_date as string}</td></tr>
              ))}</tbody></table>
          </CardContent></Card>
      )}
      {!error && data && tab === "pipeline" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">{(data as Row).count as number} open deals · {money((data as Row).total_amount)} total · {money((data as Row).weighted_amount)} weighted</p>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" data-testid="pipeline-board">
            {STAGES.map((s) => {
              const ds = rows("deals").filter((d) => d.stage === s);
              return (
                <Card key={s}><CardHeader><CardTitle>{s}</CardTitle><CardDescription>{ds.length} deals · {money(ds.reduce((t, d) => t + (d.amount as number), 0))}</CardDescription></CardHeader>
                  <CardContent className="space-y-2">{ds.map((d) => (
                    <div key={d.id as number} className="rounded-md border p-2 text-xs"><div className="font-medium">{d.name as string}</div><div className="flex justify-between text-muted-foreground"><span>{d.owner as string}</span><span className="tabular-nums">{money(d.amount)} · {d.close_date as string}</span></div></div>
                  ))}</CardContent></Card>
              );
            })}
          </div>
        </div>
      )}
      {!error && data && tab === "tickets" && (
        <Card><CardHeader><CardTitle>{(data as Row).count as number} open tickets</CardTitle><CardDescription>{(data as Row).sla_breached as number} past their first-response SLA</CardDescription></CardHeader>
          <CardContent className="overflow-x-auto p-0">
            <table className="w-full text-sm"><thead className="bg-muted/60 text-left text-xs text-muted-foreground"><tr><th className="p-2 pl-4">#</th><th className="p-2">Account</th><th className="p-2">Subject</th><th className="p-2">Priority</th><th className="p-2">Status</th><th className="p-2">SLA</th></tr></thead>
              <tbody>{rows("tickets").map((t) => (
                <tr key={t.id as number} className="border-t"><td className="p-2 pl-4 tabular-nums">{t.id as number}</td><td className="p-2">{t.account as string}</td><td className="p-2">{t.subject as string}</td>
                  <td className="p-2"><Badge variant={t.priority === "P1" ? "destructive" : t.priority === "P2" ? "warning" : "secondary"}>{t.priority as string}</Badge>{t.escalated ? <Badge variant="destructive" className="ml-1">escalated</Badge> : null}</td>
                  <td className="p-2">{t.status as string}</td><td className="p-2">{t.sla_breached ? <Badge variant="destructive">breached</Badge> : <span className="text-xs text-muted-foreground">{new Date(t.sla_due as string).toLocaleString()}</span>}</td></tr>
              ))}</tbody></table>
          </CardContent></Card>
      )}
      {!error && data && tab === "tasks" && (
        <Card><CardHeader><CardTitle>{(data as Row).count as number} open tasks</CardTitle></CardHeader>
          <CardContent className="space-y-1 text-sm">{rows("activities").map((a) => (
            <div key={a.id as number} className="flex items-center gap-2 border-t py-1.5"><span className="min-w-0 flex-1">{a.subject as string} · <span className="text-muted-foreground">{a.account as string}</span></span><span className="tabular-nums text-xs">{a.due_date as string}</span>{a.overdue ? <Badge variant="destructive">overdue</Badge> : null}</div>
          ))}</CardContent></Card>
      )}
      {!error && Array.isArray(data) && tab === "audit" && (
        <Card><CardHeader><CardTitle>Changes made through the assistant</CardTitle><CardDescription>Every write is confirmed by the user first and recorded here.</CardDescription></CardHeader>
          <CardContent className="space-y-1 text-sm" data-testid="audit-log">{(data as Row[]).map((a) => (
            <div key={a.id as number} className="flex flex-wrap items-center gap-2 border-t py-1.5"><Badge variant="outline">{a.action as string}</Badge><span>{a.target as string}</span><span className="text-xs text-muted-foreground">by {a.user_id as string} · {new Date(a.created_at as string).toLocaleString()}</span></div>
          ))}{!(data as Row[]).length && <p className="text-muted-foreground">No changes yet.</p>}</CardContent></Card>
      )}
    </div>
  );
}

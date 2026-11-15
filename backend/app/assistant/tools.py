"""Tool registry: every CRM capability the assistant can use, with a Pydantic argument schema.

The LLM only *proposes* a call as JSON; arguments are validated here, unknown tools/arguments are rejected, and the call
runs through the access-controlled service layer as the signed-in user.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..crm import service as crm


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchAccounts(_Args):
    query: str | None = Field(None, description="part of the account name")
    industry: str | None = None
    region: Literal["West", "East"] | None = None
    tier: Literal["Enterprise", "Mid-Market", "SMB"] | None = None
    max_health: int | None = Field(None, description="only accounts with health score <= this (at risk: 49)")
    renewal_within_days: int | None = Field(None, description="renewal date within the next N days")


class GetAccount(_Args):
    account: str = Field(description="account name (or id)")


class ListDeals(_Args):
    account: str | None = None
    stage: Literal["Prospecting", "Qualification", "Proposal", "Negotiation", "Closed Won", "Closed Lost"] | None = None
    open_only: bool = True
    closing_within_days: int | None = Field(None, description="close date within the next N days")
    min_amount: float | None = None
    owner: str | None = Field(None, description="rep name, only for managers/admins")


class PipelineSummary(_Args):
    group_by: Literal["stage", "owner", "month"] = "stage"


class ListTickets(_Args):
    account: str | None = None
    status: Literal["open", "pending", "resolved", "all"] = "open"
    priority: Literal["P1", "P2", "P3", "P4"] | list[Literal["P1", "P2", "P3", "P4"]] | None = Field(
        None, description='one priority or a list, e.g. ["P1", "P2"]')
    sla_breached_only: bool = False


class ListActivities(_Args):
    account: str | None = None
    kind: Literal["call", "email", "meeting", "note", "task"] | None = None
    open_tasks_only: bool = False
    overdue_only: bool = False
    due_within_days: int | None = None


# write actions (workflows) - executed only after the user confirms
class CreateTask(_Args):
    account: str
    subject: str
    due_in_days: int = Field(3, ge=0, le=365)


class LogNote(_Args):
    account: str
    text: str


class UpdateDealStage(_Args):
    deal: str | int = Field(description="deal name (or part of it) or id")
    stage: Literal["Prospecting", "Qualification", "Proposal", "Negotiation", "Closed Won", "Closed Lost"]


class EscalateTicket(_Args):
    ticket_id: int
    reason: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[_Args]
    fn: Callable[..., dict]
    write: bool = False


READ_TOOLS = [
    Tool("search_accounts", "Find accounts by name, industry, region, tier, low health (at risk) or upcoming renewal.", SearchAccounts, crm.search_accounts),
    Tool("get_account", "One account's details: owner, ARR, health, renewal, contacts, open deals, open tickets, recent activities.", GetAccount, crm.get_account),
    Tool("list_deals", "Deals (open by default) filtered by account, stage, close date window, minimum amount or owner; returns count, total and weighted amount.", ListDeals, crm.list_deals),
    Tool("pipeline_summary", "Open pipeline totals grouped by stage, owner or close month.", PipelineSummary, crm.pipeline_summary),
    Tool("list_tickets", "Support tickets (open+pending by default) by account, status, priority or SLA breach.", ListTickets, crm.list_tickets),
    Tool("list_activities", "Calls, emails, meetings, notes and tasks; open/overdue tasks or tasks due within N days.", ListActivities, crm.list_activities),
]
WRITE_TOOLS = [
    Tool("create_task", "Create a follow-up task on an account.", CreateTask, crm.create_task, write=True),
    Tool("log_note", "Log a note on an account.", LogNote, crm.log_note, write=True),
    Tool("update_deal_stage", "Move a deal to another stage.", UpdateDealStage, crm.update_deal_stage, write=True),
    Tool("escalate_ticket", "Escalate a support ticket.", EscalateTicket, crm.escalate_ticket, write=True),
]
TOOLS = {t.name: t for t in READ_TOOLS + WRITE_TOOLS}


def catalogue(tools: list[Tool]) -> str:
    lines = []
    for t in tools:
        fields = []
        for name, f in t.args.model_fields.items():
            ann = str(f.annotation).replace("typing.", "").replace("<class '", "").replace("'>", "")
            req = "required" if f.is_required() else f"default {f.default!r}"
            desc = f" - {f.description}" if f.description else ""
            fields.append(f"{name}: {ann} ({req}){desc}")
        lines.append(f"- {t.name}: {t.description}\n    args: " + "; ".join(fields))
    return "\n".join(lines)


class ToolError(Exception):
    def __init__(self, kind: str, message: str, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.kind = kind  # invalid | denied | not_found | error
        self.candidates = candidates or []


def validate_call(name: str, args: dict[str, Any] | None) -> tuple[Tool, dict]:
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError("invalid", f"unknown tool '{name}'")
    clean = {k: v for k, v in (args or {}).items() if v not in (None, "", "null")}
    try:
        parsed = tool.args(**clean)
    except ValidationError as exc:
        raise ToolError("invalid", f"invalid arguments for {name}: {exc.errors()[0]['msg']} ({exc.errors()[0]['loc']})") from exc
    return tool, parsed.model_dump(exclude_none=True)


def run(principal: crm.Principal, name: str, args: dict | None) -> dict:
    """Validate and execute one tool call. Returns a trace record; raises ToolError on failure."""
    tool, clean = validate_call(name, args)
    t0 = time.perf_counter()
    try:
        result = tool.fn(principal, **clean)
    except crm.PermissionDenied as exc:
        raise ToolError("denied", str(exc)) from exc
    except crm.NotFound as exc:
        raise ToolError("not_found", str(exc), exc.candidates) from exc
    return {"tool": name, "args": clean, "ok": True, "result": result, "ms": round((time.perf_counter() - t0) * 1000, 1)}

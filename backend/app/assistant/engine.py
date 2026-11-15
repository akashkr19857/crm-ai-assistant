"""The assistant as a LangGraph state machine.

    load memory ─▶ route (intent + standalone query) ─┬─▶ knowledge  : RAG over the docs the user may read
                                                      ├─▶ crm_data   : plan tool calls ─▶ run as the user ─▶ answer ─▶ validate
                                                      ├─▶ workflow   : plan a write action ─▶ pending, needs the user's confirmation
                                                      ├─▶ chitchat   : capabilities for the user's role
                                                      └─▶ out_of_scope: polite refusal
                                                   ─▶ save message + update entity memory
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, TypedDict

from langgraph.graph import END, StateGraph

from ..config import get_settings
from ..crm.service import Principal
from ..db import Conversation, Message, User, session
from ..llm import get_llm, parse_json
from ..rag import answer as rag_answer
from . import tools as T
from .router import Route, route
from .validation import render_fallback, validate_answer

# ---------------------------------------------------------------- prompts
PLAN_SYSTEM = """You plan CRM tool calls for an assistant. Choose the tool call(s) that fetch exactly the data needed to answer
the question. Use at most 2 calls. Use only these tools and only the listed arguments:
{catalogue}

Rules: only set filters the question explicitly asks for; never invent filter values (no industry, region, stage,
owner or account unless the question names it). "my"/"I" means the signed-in user's own records - the tools already restrict data to what the user may see, so
do not pass the user's name as owner. Use renewal_within_days / closing_within_days for "this month" (30),
"this quarter" (90), "next two weeks" (14). "At risk" accounts means max_health 49. Use get_account when one account
is named and the question is about that account in general.
Reply as JSON: {{"calls": [{{"tool": "<name>", "args": {{...}}}}]}}"""

ANSWER_SYSTEM = """You are NimbusCRM's assistant. Answer the user's question using ONLY the JSON data returned by the CRM tools.
Rules:
- Use the exact numbers, names and dates from the data. Never estimate, round up or invent values. Money as $1,234,000.
- Use the given "count", "total_amount" and "weighted_amount" fields instead of adding things up yourself.
- If a list was cut off ("count" larger than the rows shown), say you are showing the first rows of the total.
- If the data is empty, say there are no matching records.
- Write for a business user: never print raw field names such as "count", "total_amount" or JSON keys.
- Be concise: 1-3 sentences, then a short bullet list (max 8 bullets) if listing records."""

WORKFLOW_SYSTEM = """You turn a user's request into ONE CRM action for an assistant. Available actions:
{catalogue}

Fill the arguments from the request and the memory. For tasks, write a short imperative subject and convert dates to
due_in_days (tomorrow = 1, next week = 7, default 3). If something required is missing, set "missing" to a question
for the user instead of guessing.
Reply as JSON: {{"action": "<name>", "args": {{...}}, "missing": null}}"""

CHITCHAT_TEXT = {
    "sales_rep": "I can answer questions about your accounts, deals, pipeline, renewals, tasks and tickets, explain product "
                 "features and our sales policies (pricing, discounts, stages), and create tasks, log notes or update deal "
                 "stages after you confirm.",
    "sales_manager": "I can report on your team's accounts, deals and pipeline (by stage, rep or month), renewals, tasks and "
                     "tickets, explain product features and sales policies, and create tasks, log notes, update deal stages or "
                     "escalate tickets after you confirm.",
    "support_agent": "I can look up accounts and support tickets (priorities, SLA breaches), answer product and troubleshooting "
                     "questions from the help center, log notes and escalate tickets after you confirm. Deal and revenue data "
                     "is not available to the support role.",
    "admin": "I can answer questions about all CRM data and documentation, and run CRM actions after you confirm.",
}
OUT_OF_SCOPE = ("I can only help with NimbusCRM: your CRM data (accounts, deals, pipeline, tickets, tasks), product help "
                "and sales policies. Could you rephrase your question in that context?")


class State(TypedDict, total=False):
    principal: Principal
    workspaces: list[str]
    message: str
    history: list[dict]
    memory: dict
    route: Route
    answer: str
    tool_calls: list[dict]
    sources: list[dict]
    pending_action: dict
    validation: dict
    steps: list[dict]
    counter: Any
    emit: Callable[[dict], None] | None


class Counter:
    def __init__(self) -> None:
        self.calls = self.prompt = self.completion = 0

    def add(self, p: int, c: int) -> None:
        self.calls += 1
        self.prompt += p
        self.completion += c


def _step(state: State, node: str, detail: str, t0: float, **extra: Any) -> list[dict]:
    st = {"node": node, "detail": detail, "ms": round((time.perf_counter() - t0) * 1000, 1)} | extra
    if state.get("emit"):
        state["emit"]({"type": "step", "step": st})
    return state.get("steps", []) + [st]


def _chat(state: State, system: str, user: str, *, json_mode: bool, max_tokens: int, temperature: float = 0.0) -> str:
    res = get_llm().chat([{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=json_mode,
                         temperature=temperature, max_tokens=max_tokens)
    state["counter"].add(res.prompt_tokens, res.completion_tokens)
    return res.text


# ---------------------------------------------------------------- nodes
def n_route(state: State) -> State:
    t0 = time.perf_counter()
    r = route(state["message"], state.get("history", []), state.get("memory", {}), get_settings().history_turns, state["counter"])
    return {"route": r, "steps": _step(state, "route", f"intent={r.intent} ({r.source})", t0, intent=r.intent,
                                       query=r.standalone_query)}


def n_knowledge(state: State) -> State:
    t0 = time.perf_counter()
    r = state["route"]
    ans = rag_answer(r.standalone_query, state["workspaces"])
    if ans.llm:
        state["counter"].add(ans.llm.prompt_tokens, ans.llm.completion_tokens)
    sources = [{"n": c["n"], "doc_id": c["doc_id"], "doc_title": c["doc_title"], "page": c["page"], "chunk_id": c["chunk_id"],
                "text": next((h.text[:500] for h in ans.hits if h.chunk_id == c["chunk_id"]), "")} for c in ans.citations]
    validation = {"checked": True, "passed": ans.not_found or bool(sources),
                  "issues": [] if (ans.not_found or sources) else ["answer has no citation"], "not_found": ans.not_found}
    return {"answer": ans.answer, "sources": sources, "validation": validation,
            "steps": _step(state, "knowledge", f"{len(ans.hits)} passages, {len(sources)} cited" + (" (not found)" if ans.not_found else ""), t0)}


def _plan_calls(state: State, query: str, catalogue_tools: list[T.Tool]) -> list[dict]:
    mem = json.dumps(state.get("memory", {}))
    text = _chat(state, PLAN_SYSTEM.format(catalogue=T.catalogue(catalogue_tools)),
                 f"Signed-in user: {state['principal'].name} ({state['principal'].crm_role})\nMemory: {mem}\nQuestion: {query}",
                 json_mode=True, max_tokens=300)
    calls = parse_json(text).get("calls", [])
    return [c for c in calls if isinstance(c, dict) and c.get("tool")][:2] if isinstance(calls, list) else []


def n_crm(state: State) -> State:
    t0 = time.perf_counter()
    p, query = state["principal"], state["route"].standalone_query
    calls = _plan_calls(state, query, T.READ_TOOLS)
    steps = _step(state, "plan", ", ".join(f"{c['tool']}({json.dumps(c.get('args', {}))})" for c in calls) or "no tool chosen", t0)
    if not calls and state["route"].account:
        calls = [{"tool": "get_account", "args": {"account": state["route"].account}}]
    trace: list[dict] = []
    for c in calls:
        t1 = time.perf_counter()
        try:
            rec = T.run(p, c["tool"], c.get("args"))
            trace.append(rec)
            steps = _step({**state, "steps": steps}, "tool", f"{c['tool']} ok", t1, tool=c["tool"])
        except T.ToolError as exc:
            trace.append({"tool": c["tool"], "args": c.get("args", {}), "ok": False, "error": str(exc), "kind": exc.kind,
                          "candidates": exc.candidates, "ms": round((time.perf_counter() - t1) * 1000, 1)})
            steps = _step({**state, "steps": steps}, "tool", f"{c['tool']} {exc.kind}: {exc}", t1, tool=c["tool"])
    ok = [t for t in trace if t["ok"]]
    denied = next((t for t in trace if t.get("kind") == "denied"), None)
    missing = next((t for t in trace if t.get("kind") == "not_found"), None)
    if not ok:
        if denied:
            msg = f"I can't show that: {denied['error']}"
        elif missing:
            msg = f"{missing['error']}" + (f" Did you mean: {', '.join(missing['candidates'])}?" if missing["candidates"] else "")
        else:
            msg = "I couldn't find a way to look that up in the CRM. Could you rephrase it, for example with an account name?"
        return {"answer": msg, "tool_calls": trace, "validation": {"checked": False, "passed": True, "issues": []}, "steps": steps}
    data = json.dumps([{"tool": t["tool"], "args": t["args"], "result": t["result"]} for t in ok], default=str)[:12000]
    user = f"Question: {query}\n\nCRM data (JSON):\n{data}"
    t2 = time.perf_counter()
    text = _chat(state, ANSWER_SYSTEM, user, json_mode=False, max_tokens=500)
    validation = {"checked": False, "passed": True, "issues": [], "regenerated": False, "fallback": False}
    if get_settings().validate_answers:
        results = [t["result"] for t in ok]
        v = validate_answer(text, results, query)
        validation.update(v)
        if not v["passed"]:
            validation["first_issues"] = v["issues"]
            retry = _chat(state, ANSWER_SYSTEM, user + "\n\nYour previous answer contained values that are not in the data: "
                          + "; ".join(v["issues"]) + ". Answer again using only values that appear in the data.",
                          json_mode=False, max_tokens=500)
            v2 = validate_answer(retry, results, query)
            validation.update(v2, regenerated=True)
            if v2["passed"]:
                text = retry
            else:
                text, validation["fallback"] = render_fallback(ok), True
                validation["passed"] = True
    if denied:
        text += f"\n\nNote: part of the request was not available to your role ({denied['error']})"
    return {"answer": text.strip(), "tool_calls": trace, "validation": validation,
            "steps": _step({**state, "steps": steps}, "answer", "validated" if validation.get("checked") else "answered", t2,
                           validation=validation)}


def n_workflow(state: State) -> State:
    t0 = time.perf_counter()
    mem = json.dumps(state.get("memory", {}))
    text = _chat(state, WORKFLOW_SYSTEM.format(catalogue=T.catalogue(T.WRITE_TOOLS)),
                 f"Memory: {mem}\nRequest: {state['route'].standalone_query}", json_mode=True, max_tokens=300)
    data = parse_json(text)
    action, args = str(data.get("action", "")), data.get("args") or {}
    if data.get("missing"):
        return {"answer": str(data["missing"]), "steps": _step(state, "workflow", "needs more information", t0)}
    try:
        tool, clean = T.validate_call(action, args)
        if not tool.write:
            raise T.ToolError("invalid", f"{action} is not an action")
    except T.ToolError as exc:
        return {"answer": f"I couldn't prepare that action ({exc}). Could you say it differently?",
                "steps": _step(state, "workflow", f"invalid: {exc}", t0)}
    summary = describe_action(action, clean)
    return {"answer": f"I'm ready to **{summary}**. Please confirm.", "pending_action": {"action": action, "args": clean, "summary": summary},
            "steps": _step(state, "workflow", f"pending {action}", t0, action=action, args=clean)}


def describe_action(action: str, a: dict) -> str:
    return {"create_task": lambda: f"create a task \"{a['subject']}\" on {a['account']} due in {a.get('due_in_days', 3)} day(s)",
            "log_note": lambda: f"log a note on {a['account']}: \"{a['text']}\"",
            "update_deal_stage": lambda: f"move the deal \"{a['deal']}\" to {a['stage']}",
            "escalate_ticket": lambda: f"escalate ticket #{a['ticket_id']}" + (f" ({a['reason']})" if a.get("reason") else "")}[action]()


def n_chitchat(state: State) -> State:
    return {"answer": f"Hi {state['principal'].name.split()[0]}! " + CHITCHAT_TEXT.get(state["principal"].crm_role, CHITCHAT_TEXT["admin"]),
            "steps": _step(state, "chitchat", "capabilities for role", time.perf_counter())}


def n_out_of_scope(state: State) -> State:
    return {"answer": OUT_OF_SCOPE, "steps": _step(state, "out_of_scope", "refused", time.perf_counter())}


def _build():
    g = StateGraph(State)
    for name, fn in (("route", n_route), ("knowledge", n_knowledge), ("crm_data", n_crm), ("workflow", n_workflow),
                     ("chitchat", n_chitchat), ("out_of_scope", n_out_of_scope)):
        g.add_node(name, fn)
    g.set_entry_point("route")
    g.add_conditional_edges("route", lambda s: s["route"].intent,
                            {i: i for i in ("knowledge", "crm_data", "workflow", "chitchat", "out_of_scope")})
    for n in ("knowledge", "crm_data", "workflow", "chitchat", "out_of_scope"):
        g.add_edge(n, END)
    return g.compile()


GRAPH = _build()


# ---------------------------------------------------------------- public API
def _update_memory(memory: dict, r: Route, tool_calls: list[dict], pending: dict) -> dict:
    mem = dict(memory)
    if r.account:
        mem["account"] = r.account
    for t in tool_calls:
        if not t.get("ok"):
            continue
        res = t["result"]
        if t["tool"] == "get_account":
            mem["account"] = res["account"]["name"]
        for key, mkey in (("deals", "deal"), ("tickets", "ticket"), ("accounts", "account")):
            rows = res.get(key)
            if isinstance(rows, list) and len(rows) == 1:
                mem[mkey] = rows[0].get("name") or (f"#{rows[0]['id']}" if mkey == "ticket" else None)
                if mkey == "ticket":
                    mem["ticket_id"] = rows[0]["id"]
    if pending.get("args", {}).get("account"):
        mem["account"] = pending["args"]["account"]
    return {k: v for k, v in mem.items() if v}


def ask(user_id: str, conversation_id: int, content: str, emit: Callable[[dict], None] | None = None) -> Message:
    t0 = time.perf_counter()
    with session() as db:
        user = db.get(User, user_id)
        conv = db.get(Conversation, conversation_id)
        if user is None or conv is None or conv.user_id != user_id:
            raise PermissionError("conversation not found")
        history = [{"role": m.role, "content": m.content} for m in
                   db.query(Message).filter(Message.conversation_id == conv.id).order_by(Message.id).all()]
        principal, workspaces, memory = Principal.from_user(user), user.workspace_ids, dict(conv.memory or {})
        db.add(Message(conversation_id=conv.id, user_id=user_id, role="user", content=content))
        if not history:
            conv.title = content[:80]
        db.commit()
    counter = Counter()
    state = GRAPH.invoke({"principal": principal, "workspaces": workspaces, "message": content, "history": history,
                          "memory": memory, "steps": [], "counter": counter, "emit": emit})
    r: Route = state["route"]
    tool_calls = state.get("tool_calls", [])
    pending = state.get("pending_action", {})
    with session() as db:
        conv = db.get(Conversation, conversation_id)
        conv.memory = _update_memory(memory, r, tool_calls, pending)
        msg = Message(conversation_id=conversation_id, user_id=user_id, role="assistant", content=state.get("answer", ""),
                      intent=r.intent, route=r.source, standalone_query=r.standalone_query,
                      tool_calls=[_compact(t) for t in tool_calls], sources=state.get("sources", []), pending_action=pending,
                      validation=state.get("validation", {}), latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                      llm_calls=counter.calls, prompt_tokens=counter.prompt, completion_tokens=counter.completion,
                      steps=state.get("steps", []))
        db.add(msg)
        db.commit()
        return msg


def _compact(t: dict) -> dict:
    """Keep the trace bounded: counts plus up to 50 rows (the tools return at most 20, so in practice nothing is cut)."""
    out = {k: v for k, v in t.items() if k != "result"}
    if t.get("ok"):
        res = t["result"]
        slim = {}
        for k, v in res.items():
            slim[k] = v[:50] if isinstance(v, list) else v
        out["result"] = slim
    return out


def confirm(user_id: str, message_id: int, accept: bool) -> Message:
    """Run (or cancel) the write action a previous assistant message proposed."""
    with session() as db:
        msg = db.get(Message, message_id)
        if msg is None or msg.user_id != user_id or not msg.pending_action or msg.pending_action.get("status"):
            raise PermissionError("no pending action")
        user = db.get(User, user_id)
        principal = Principal.from_user(user)
        action = dict(msg.pending_action)
    t0 = time.perf_counter()
    if not accept:
        result_text, status, rec = "Cancelled. Nothing was changed.", "cancelled", None
    else:
        try:
            rec = T.run(principal, action["action"], action["args"])
            r = rec["result"]
            result_text = "Done: " + {"create_task": lambda: f"task #{r['id']} \"{r['subject']}\" created on {r['account']}, due {r['due_date']}.",
                                      "log_note": lambda: f"note #{r['id']} logged on {r['account']}.",
                                      "update_deal_stage": lambda: f"\"{r['deal']}\" moved from {r['from']} to {r['to']}.",
                                      "escalate_ticket": lambda: f"ticket #{r['escalated']} escalated (now {r['priority']})."}[action["action"]]()
            status = "done"
        except T.ToolError as exc:
            result_text, status, rec = f"I couldn't do that: {exc}", "failed", None
    with session() as db:
        msg = db.get(Message, message_id)
        msg.pending_action = action | {"status": status}
        out = Message(conversation_id=msg.conversation_id, user_id=user_id, role="assistant", content=result_text,
                      intent="workflow", route="confirm", tool_calls=[_compact(rec)] if rec else [],
                      latency_ms=round((time.perf_counter() - t0) * 1000, 1))
        db.add(out)
        db.commit()
        return out


def new_conversation(user_id: str) -> Conversation:
    with session() as db:
        c = Conversation(user_id=user_id)
        db.add(c)
        db.commit()
        return c


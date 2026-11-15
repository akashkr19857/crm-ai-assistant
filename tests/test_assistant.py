"""CRM access control, tools, validation, routing and the full assistant graph with a scripted fake LLM."""
from __future__ import annotations

import json

import pytest
from conftest import auth

from app.assistant import tools as T
from app.assistant.router import keyword_intent
from app.assistant.validation import dates_in, numbers_in, render_fallback, validate_answer
from app.crm import service as crm
from app.db import AuditLog, Deal, User, session
from app.llm import FakeLLM, set_llm


def P(uid: str) -> crm.Principal:
    with session() as db:
        return crm.Principal.from_user(db.get(User, uid))


# ---------------------------------------------------------------- CRM access control
def test_row_level_access(seeded):
    rep, mgr, sup, adm = P("ananya"), P("vikram"), P("lena"), P("admin")
    mine = crm.search_accounts(rep, limit=100)
    team = crm.search_accounts(mgr, limit=100)
    assert mine["count"] == 10 and team["count"] == 20 and crm.search_accounts(adm, limit=100)["count"] == 40
    assert {a["owner"] for a in mine["accounts"]} == {"Ananya Rao"}
    assert {a["region"] for a in team["accounts"]} == {"West"}
    assert crm.search_accounts(sup, limit=100)["count"] == 40


def test_support_cannot_see_revenue(seeded):
    sup = P("lena")
    rows = crm.search_accounts(sup)["accounts"]
    assert rows and all("arr" not in r for r in rows) and "total_arr" not in crm.search_accounts(sup)
    for fn in (crm.pipeline_summary, crm.list_deals):
        with pytest.raises(crm.PermissionDenied):
            fn(sup)
    assert "open_deals" not in crm.get_account(sup, rows[0]["name"])


def test_rep_cannot_open_other_reps_account(seeded):
    east = crm.search_accounts(P("admin"), region="East")["accounts"][0]["name"]
    with pytest.raises(crm.PermissionDenied):
        crm.get_account(P("ananya"), east)
    with pytest.raises(crm.NotFound):
        crm.get_account(P("ananya"), "Totally Unknown Corp")


def test_pipeline_totals_are_consistent(seeded):
    p = P("vikram")
    summary = crm.pipeline_summary(p)
    deals = crm.list_deals(p, limit=500)
    assert summary["count"] == deals["count"] and abs(summary["total_amount"] - deals["total_amount"]) < 0.01
    assert sum(g["count"] for g in summary["groups"]) == summary["count"]


def test_write_actions_are_audited_and_scoped(seeded):
    rep = P("ananya")
    acc = crm.search_accounts(rep)["accounts"][0]["name"]
    r = crm.create_task(rep, acc, "Send renewal quote", 7)
    assert r["account"] == acc
    with session() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "create_task", AuditLog.user_id == "ananya").count() >= 1
    with pytest.raises(crm.PermissionDenied):
        crm.update_deal_stage(P("lena"), 1, "Negotiation")
    with pytest.raises(crm.PermissionDenied):
        crm.escalate_ticket(rep, 1)


# ---------------------------------------------------------------- tools + validation
def test_tool_argument_validation():
    with pytest.raises(T.ToolError) as e:
        T.validate_call("drop_database", {})
    assert e.value.kind == "invalid"
    with pytest.raises(T.ToolError):
        T.validate_call("list_deals", {"stage": "Signed"})
    with pytest.raises(T.ToolError):
        T.validate_call("search_accounts", {"unknown_arg": 1})
    _, clean = T.validate_call("list_tickets", {"priority": ["P1", "P2"], "account": None})
    assert clean["priority"] == ["P1", "P2"] and "account" not in clean


def test_tool_run_maps_permission_errors(seeded):
    with pytest.raises(T.ToolError) as e:
        T.run(P("lena"), "pipeline_summary", {})
    assert e.value.kind == "denied"


def test_number_and_date_extraction():
    assert [v for _, v, _ in numbers_in("Pipeline $1,234,000 (about $1.2M), 3 deals, P1 tickets, 4.5k")] == [1234000.0, 1200000.0, 3.0, 4500.0]
    assert {iso for _, iso in dates_in("renews December 1, 2026 or 2026-12-05")} == {"2026-12-01", "2026-12-05"}


def test_validator_catches_invented_numbers_and_dates():
    data = [{"count": 8, "total_amount": 982000.0, "deals": [{"amount": 120000.0, "close_date": "2026-12-01", "probability": 50}]}]
    ok = validate_answer("You have 8 open deals worth $982,000 (about $1.0M); the largest is $120K, closing December 1, 2026 at 50%.", data)
    assert ok["passed"], ok
    bad = validate_answer("You have 9 open deals worth $1.5M closing 2026-12-05.", data)
    assert not bad["passed"] and len(bad["issues"]) == 3


def test_fallback_renders_only_data():
    text = render_fallback([{"tool": "list_deals", "result": {"count": 2, "total_amount": 30000.0,
                                                                "deals": [{"name": "A - X", "account": "A", "stage": "Proposal", "amount": 10000.0},
                                                                          {"name": "B - Y", "account": "B", "stage": "Negotiation", "amount": 20000.0}]}}])
    assert "2 deals, $30,000 total" in text and "$20,000" in text
    assert validate_answer(text, [{"count": 2, "total_amount": 30000.0, "deals": [{"amount": 10000.0}, {"amount": 20000.0}]}])["passed"]


@pytest.mark.parametrize("text,intent", [
    ("hello there", "chitchat"),
    ("create a task to call Cedar Energy tomorrow", "workflow"),
    ("how do I fix SSO login loop", "knowledge"),
    ("show my open deals", "crm_data"),
    ("what's the capital of France", "out_of_scope"),
])
def test_keyword_router_fallback(text, intent):
    assert keyword_intent(text) == intent


# ---------------------------------------------------------------- full graph with a scripted LLM
class Script:
    """Fake LLM: answers per prompt type, records calls."""

    def __init__(self, route: dict, plan: dict | None = None, answers: list[str] | None = None, workflow: dict | None = None):
        self.route, self.plan, self.answers, self.workflow = route, plan or {"calls": []}, list(answers or []), workflow or {}

    def __call__(self, messages: list[dict]) -> str:
        system = messages[0]["content"]
        if "router of an AI assistant" in system:
            return json.dumps(self.route)
        if "plan CRM tool calls" in system:
            return json.dumps(self.plan)
        if "turn a user's request into ONE CRM action" in system:
            return json.dumps(self.workflow)
        if "Answer the user's question using ONLY the JSON data" in system:
            return self.answers.pop(0) if self.answers else "No answer."
        return "Docs say so [1]."


def _ask(user: str, script: Script, text: str, conv_id: int | None = None):
    from app.assistant.engine import ask, new_conversation

    set_llm(FakeLLM(script))
    try:
        cid = conv_id or new_conversation(user).id
        return ask(user, cid, text), cid
    finally:
        set_llm(None)


def test_crm_question_validated_first_time(seeded):
    p = crm.pipeline_summary(P("ananya"))
    good = f"Your open pipeline is {p['count']} deals worth ${p['total_amount']:,.0f}."
    m, _ = _ask("ananya", Script({"intent": "crm_data", "standalone_query": "my pipeline", "account": None},
                                 {"calls": [{"tool": "pipeline_summary", "args": {}}]}, [good]), "What's my pipeline?")
    assert m.intent == "crm_data" and m.content == good
    assert m.validation["passed"] and not m.validation.get("regenerated") and m.llm_calls == 3
    assert m.tool_calls[0]["tool"] == "pipeline_summary" and m.tool_calls[0]["ok"]


def test_hallucinated_numbers_are_regenerated_then_fall_back(seeded):
    p = crm.pipeline_summary(P("ananya"))
    good = f"You have {p['count']} open deals worth ${p['total_amount']:,.0f}."
    m, _ = _ask("ananya", Script({"intent": "crm_data", "standalone_query": "my pipeline", "account": None},
                                 {"calls": [{"tool": "pipeline_summary", "args": {}}]}, ["You have 999 deals worth $77,777,777.", good]),
                "What's my pipeline?")
    assert m.validation["regenerated"] and m.validation["first_issues"] and m.content == good and not m.validation["fallback"]
    m2, _ = _ask("ananya", Script({"intent": "crm_data", "standalone_query": "my pipeline", "account": None},
                                  {"calls": [{"tool": "pipeline_summary", "args": {}}]}, ["999 deals.", "Still 999 deals."]),
                 "What's my pipeline?")
    assert m2.validation["fallback"] and "999" not in m2.content and f"{p['count']} deals" in m2.content


def test_multi_turn_memory_resolves_follow_ups(seeded):
    acc = crm.search_accounts(P("ananya"))["accounts"][0]["name"]
    m1, cid = _ask("ananya", Script({"intent": "crm_data", "standalone_query": f"Tell me about {acc}", "account": acc},
                                    {"calls": [{"tool": "get_account", "args": {"account": acc}}]}, [f"{acc} is an account."]),
                   f"Tell me about {acc}")
    from app.db import Conversation

    with session() as db:
        assert db.get(Conversation, cid).memory["account"] == acc
    captured = {}

    def router(messages):
        if "router of an AI assistant" in messages[0]["content"]:
            captured["user"] = messages[-1]["content"]
        return Script({"intent": "crm_data", "standalone_query": f"open tickets for {acc}", "account": acc},
                      {"calls": [{"tool": "list_tickets", "args": {"account": acc}}]}, ["No open tickets."])(messages)

    set_llm(FakeLLM(router))
    try:
        from app.assistant.engine import ask

        m2 = ask("ananya", cid, "what about their open tickets?")
    finally:
        set_llm(None)
    assert f"account: {acc}" in captured["user"] and "Tell me about" in captured["user"]  # memory + history reach the router
    assert m2.standalone_query == f"open tickets for {acc}" and m2.tool_calls[0]["args"]["account"] == acc


def test_support_role_denied_pipeline(seeded):
    m, _ = _ask("lena", Script({"intent": "crm_data", "standalone_query": "pipeline", "account": None},
                               {"calls": [{"tool": "pipeline_summary", "args": {}}]}), "Show me the pipeline")
    assert "can't show" in m.content and m.tool_calls[0]["kind"] == "denied" and m.llm_calls == 2


def test_knowledge_route_cites_documents(seeded):
    m, _ = _ask("admin", Script({"intent": "knowledge", "standalone_query": "Which function is cross-cutting?", "account": None}),
                "Which function is cross-cutting?")
    assert m.intent == "knowledge" and m.sources and m.sources[0]["doc_title"] == "AI Risk Test Doc"


def test_knowledge_respects_document_workspaces(seeded):
    m, _ = _ask("lena", Script({"intent": "knowledge", "standalone_query": "back up data integrity before restoring", "account": None}),
                "How should backups be verified?")
    assert all(s["doc_title"] != "Security Test Doc" for s in m.sources)  # playbook doc is not in lena's workspaces


def test_workflow_needs_confirmation_then_writes(seeded, client):
    from app.assistant.engine import confirm

    rep = P("ananya")
    deal = crm.list_deals(rep, limit=1)["deals"][0]
    m, _ = _ask("ananya", Script({"intent": "workflow", "standalone_query": "move deal", "account": None},
                                 workflow={"action": "update_deal_stage", "args": {"deal": deal["id"], "stage": "Negotiation"}, "missing": None}),
                f"Move {deal['name']} to negotiation")
    assert m.pending_action["action"] == "update_deal_stage" and "Please confirm" in m.content
    with session() as db:
        assert db.get(Deal, deal["id"]).stage == deal["stage"]  # nothing changed before confirmation
    done = confirm("ananya", m.id, True)
    assert done.content.startswith("Done") and "Negotiation" in done.content
    with session() as db:
        assert db.get(Deal, deal["id"]).stage == "Negotiation"
    with pytest.raises(PermissionError):
        confirm("ananya", m.id, True)  # cannot run twice
    r = client.get("/api/crm/audit", headers=auth("ananya")).json()
    assert any(a["action"] == "update_deal_stage" for a in r)


def test_workflow_cancel_and_invalid(seeded):
    from app.assistant.engine import confirm

    m, _ = _ask("ananya", Script({"intent": "workflow", "standalone_query": "x", "account": None},
                                 workflow={"action": "create_task", "args": {"account": "Anything", "subject": "Call", "due_in_days": 2}}),
                "remind me to call")
    assert confirm("ananya", m.id, False).content.startswith("Cancelled")
    bad, _ = _ask("ananya", Script({"intent": "workflow", "standalone_query": "x", "account": None},
                                   workflow={"action": "delete_everything", "args": {}}), "delete all deals")
    assert "couldn't prepare" in bad.content and not bad.pending_action


def test_out_of_scope_and_chitchat_need_no_tools(seeded):
    m, _ = _ask("ananya", Script({"intent": "out_of_scope", "standalone_query": "weather", "account": None}), "weather in Paris?")
    assert "only help with NimbusCRM" in m.content and not m.tool_calls and m.llm_calls == 1
    c, _ = _ask("lena", Script({"intent": "chitchat", "standalone_query": "hi", "account": None}), "hi")
    assert "not available to the support role" in c.content


# ---------------------------------------------------------------- API
def test_conversation_api_and_isolation(client, seeded):
    set_llm(FakeLLM(Script({"intent": "chitchat", "standalone_query": "hi", "account": None})))
    try:
        conv = client.post("/api/conversations", headers=auth("ananya")).json()
        msg = client.post(f"/api/conversations/{conv['id']}/messages", headers=auth("ananya"), json={"content": "hello"}).json()
    finally:
        set_llm(None)
    assert msg["role"] == "assistant" and msg["intent"] == "chitchat"
    assert len(client.get(f"/api/conversations/{conv['id']}/messages", headers=auth("ananya")).json()) == 2
    assert client.get(f"/api/conversations/{conv['id']}/messages", headers=auth("vikram")).status_code == 404
    assert client.post(f"/api/conversations/{conv['id']}/messages", headers=auth("lena"), json={"content": "x"}).status_code == 404


def test_websocket_streams_steps(client, seeded):
    set_llm(FakeLLM(Script({"intent": "chitchat", "standalone_query": "hi", "account": None})))
    try:
        conv = client.post("/api/conversations", headers=auth("vikram")).json()
        with client.websocket_connect("/ws/assistant?token=demo-vikram") as ws:
            ws.send_text(json.dumps({"conversation_id": conv["id"], "content": "hi"}))
            events = []
            while True:
                ev = ws.receive_json()
                events.append(ev)
                if ev["type"] in ("final", "error"):
                    break
    finally:
        set_llm(None)
    assert [e["step"]["node"] for e in events if e["type"] == "step"] == ["route", "chitchat"]
    assert events[-1]["type"] == "final" and events[-1]["message"]["intent"] == "chitchat"


def test_crm_api_enforces_roles(client, seeded):
    assert client.get("/api/crm/accounts", headers=auth("ananya")).json()["count"] == 10
    assert client.get("/api/crm/pipeline", headers=auth("lena")).status_code == 403
    assert client.get("/api/crm/deals", headers=auth("vikram")).json()["count"] > 0
    assert client.get("/api/users").json()[0]["id"] == "ananya" and len(client.get("/api/users").json()) == 4


def test_analytics_counts_intents(client, seeded):
    a = client.get("/api/analytics", headers=auth("admin")).json()
    assert a["messages"] > 0 and {"chitchat", "crm_data"} <= {i["intent"] for i in a["intents"]}
    assert a["access_denied"] >= 1


def test_validator_accepts_digits_inside_text_values():
    data = [{"contacts": [{"name": "Owen Nair", "phone": "+1-555-937-9747", "email": "owen.nair@cedar.example.com"}]}]
    assert validate_answer("The primary contact is Owen Nair (+1-555-937-9747).", data)["passed"]
    assert not validate_answer("Call Owen Nair on +1-555-111-2222.", data)["passed"]

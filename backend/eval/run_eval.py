"""Evaluate the CRM assistant end to end with the local LLM.

    python backend/eval/run_eval.py --release v1.0.0 --label baseline

Expected answers for CRM questions are computed with direct SQL on the demo database (independent of the assistant's
tool code), so the numbers are checked against ground truth, not against the tools themselves.

Measures:
* intent accuracy (all cases)                       * tool selection accuracy (CRM data cases)
* CRM answer accuracy (expected values in answer)   * multi-turn follow-up accuracy
* knowledge answer accuracy + not-found handling    * access-control leaks (must be 0)
* workflow action accuracy (action + key argument; nothing executed)
* hallucinated values in final CRM answers WITH vs WITHOUT response validation
* latency p50/p95 per intent and LLM calls per answer
"""
from __future__ import annotations

import argparse
import json
import logging
import platform
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0]))

from app.assistant.engine import ask, new_conversation  # noqa: E402
from app.assistant.validation import validate_answer  # noqa: E402
from app.config import PROJECT_ROOT, get_settings  # noqa: E402
from app.db import Account, Activity, Contact, Deal, EvalRun, Ticket, session  # noqa: E402
from app.seed import seed_all_crm  # noqa: E402

from contextlib import nullcontext as gpu_lock  # single-process: no cross-process GPU lock needed

log = logging.getLogger("eval")
RESULTS_DIR = HERE / "results"
OPEN = ("Prospecting", "Qualification", "Proposal", "Negotiation")
TEAM = {"ananya": ["ananya"], "vikram": ["ananya", "james"], "admin": ["ananya", "james", "sofia", "marcus"]}


# ---------------------------------------------------------------- ground truth (direct SQL)
def money_variants(v: float) -> list[str]:
    out = [f"{v:,.0f}", f"{v:,.2f}"]
    if v >= 1_000_000:
        out += [f"{v / 1e6:.1f}M", f"{v / 1e6:.2f}M", f"{v / 1e6:.1f} million", f"{v / 1e6:.2f} million"]
    elif v >= 1000:
        out += [f"{v / 1e3:.0f}K", f"{v / 1e3:.0f}k", f"{v / 1e3:.0f},000"]
    return out


def has_any(text: str, variants: list[str]) -> bool:
    t = text.lower().replace(" ", " ")
    return any(v.lower() in t for v in variants)


def truth() -> dict:
    today = date.today()
    with session() as db:
        accs = db.query(Account).all()
        deals = db.query(Deal).all()
        tickets = db.query(Ticket).all()
        acts = db.query(Activity).all()
        contacts = db.query(Contact).all()
    by_acc = {a.id: a for a in accs}
    T: dict = {}
    for u, owners in TEAM.items():
        od = [d for d in deals if d.stage in OPEN and by_acc[d.account_id].owner_id in owners]
        T[u] = {"open_count": len(od), "open_total": sum(d.amount for d in od),
                "by_owner": {}, "closing30": sorted(d.name for d in od if today <= d.close_date <= today + timedelta(days=30)),
                "at_risk": sorted(a.name for a in accs if a.owner_id in owners and a.health_score <= 49),
                "renew60": sorted(a.name for a in accs if a.owner_id in owners and a.renewal_date and today <= a.renewal_date <= today + timedelta(days=60))}
        for d in od:
            T[u]["by_owner"][d.owner_id] = T[u]["by_owner"].get(d.owner_id, 0) + d.amount
    mine = sorted([a for a in accs if a.owner_id == "ananya"], key=lambda a: a.name)
    focus = mine[0]
    T["focus"] = {"name": focus.name, "health": focus.health_score,
                  "open_tickets": sum(1 for t in tickets if t.account_id == focus.id and t.status != "resolved"),
                  "primary": next((c.name for c in contacts if c.account_id == focus.id and c.is_primary), None)}
    second = mine[1]
    T["focus2"] = {"name": second.name, "arr": second.arr,
                   "open_deals": sum(1 for d in deals if d.account_id == second.id and d.stage in OPEN)}
    now = datetime.now(timezone.utc)

    def breached(t: Ticket) -> bool:
        sla = t.sla_due if t.sla_due.tzinfo else t.sla_due.replace(tzinfo=timezone.utc)
        return t.status != "resolved" and sla < now

    T["support"] = {"open_p1": sum(1 for t in tickets if t.status in ("open", "pending") and t.priority == "P1"),
                    "open_p1p2": sum(1 for t in tickets if t.status in ("open", "pending") and t.priority in ("P1", "P2")),
                    "breached": sum(1 for t in tickets if t.status in ("open", "pending") and breached(t))}
    T["admin_extra"] = {"enterprise_arr": sum(a.arr for a in accs if a.tier == "Enterprise"),
                        "overdue_tasks": sum(1 for a in acts if a.kind == "task" and not a.done and a.due_date and a.due_date < today)}
    T["east_account"] = sorted(a.name for a in accs if a.region == "East")[0]
    return T


# ---------------------------------------------------------------- cases
def build_cases(T: dict) -> list[dict]:
    f, f2, s, x = T["focus"], T["focus2"], T["support"], T["admin_extra"]
    a, v = T["ananya"], T["vikram"]
    owners = {"ananya": "Ananya", "james": "James"}
    crm = [
        dict(user="ananya", q="How many open deals do I have and what are they worth in total?", tool="list_deals|pipeline_summary",
             expect=[[str(a["open_count"])], money_variants(a["open_total"])]),
        dict(user="ananya", q="Which of my accounts renew in the next 60 days?", tool="search_accounts", expect=[[n] for n in a["renew60"]]),
        dict(user="ananya", q=f"What is the health score of {f['name']}?", tool="get_account|search_accounts", expect=[[str(f["health"])]]),
        dict(user="ananya", q=f"Who is the primary contact at {f['name']}?", tool="get_account", expect=[[f["primary"]]]),
        dict(user="ananya", q=f"What is the ARR of {f2['name']}?", tool="get_account|search_accounts", expect=[money_variants(f2["arr"])]),
        dict(user="vikram", q="Show my team's open pipeline by owner", tool="pipeline_summary|list_deals",
             expect=[[owners[o]] + [] for o in v["by_owner"]] + [money_variants(t) for t in v["by_owner"].values()]),
        dict(user="vikram", q="Which accounts in my team are at risk?", tool="search_accounts", expect=[[n] for n in v["at_risk"]] or [["no"]]),
        dict(user="vikram", q="List the deals closing in the next 30 days", tool="list_deals",
             expect=[[n.split(" - ")[0]] for n in v["closing30"]] or [["no"]]),
        dict(user="lena", q="How many open P1 tickets are there?", tool="list_tickets", expect=[[str(s["open_p1"])]]),
        dict(user="lena", q="How many open P1 or P2 tickets are there?", tool="list_tickets", expect=[[str(s["open_p1p2"])]]),
        dict(user="lena", q="How many open tickets have breached their SLA?", tool="list_tickets", expect=[[str(s["breached"])]]),
        dict(user="admin", q="What is the total ARR of our Enterprise accounts?", tool="search_accounts", expect=[money_variants(x["enterprise_arr"])]),
        dict(user="admin", q="How many open tasks are overdue?", tool="list_activities", expect=[[str(x["overdue_tasks"])]]),
    ]
    for c in crm:
        c["kind"], c["intent"] = "crm", "crm_data"
    knowledge = [
        ("ananya", "Who has to approve a 15% discount?", [["sales manager"]]),
        ("ananya", "What does the Professional edition cost?", [["65"]]),
        ("ananya", "How many days before the renewal date should renewal conversations start?", [["90"]]),
        ("ananya", "What probability does the Negotiation stage carry?", [["75"]]),
        ("lena", "What is the first-response SLA for a P2 ticket?", [["8 hours", "8-hour", "eight hours"]]),
        ("lena", "How do I fix an SSO login loop after a password reset?", [["cookie"]]),
        ("lena", "What is the API rate limit on the Enterprise edition?", [["1,000", "1000"]]),
        ("vikram", "How many workflow rules does the Professional edition allow?", [["50"]]),
        ("vikram", "How large can a CSV import file be?", [["50,000", "50000"]]),
        ("admin", "Does NimbusCRM integrate with SAP S/4HANA?", None),
    ]
    k = [dict(kind="knowledge", intent="knowledge", user=u, q=q, expect=e) for u, q, e in knowledge]
    rbac = [
        dict(kind="rbac", intent="crm_data", user="lena", q="What is our total open pipeline?", forbid=money_variants(T["admin"]["open_total"])),
        dict(kind="rbac", intent="crm_data", user="lena", q=f"What deals are open for {f['name']}?", forbid=["Prospecting", "Proposal", "Negotiation", "Qualification"]),
        dict(kind="rbac", intent="crm_data", user="ananya", q=f"Tell me about {T['east_account']}", forbid=["health score", "ARR"]),
        dict(kind="rbac", intent="knowledge", user="lena", q="What does the Professional edition cost per user?", forbid=["$65", "65 per user"]),
    ]
    workflow = [
        dict(user="ananya", q=f"Create a task on {f['name']} to send the renewal quote next week", action="create_task", key=("account", f["name"])),
        dict(user="ananya", q=f"Log a note on {f2['name']}: the champion is moving to a new role", action="log_note", key=("account", f2["name"])),
        dict(user="vikram", q="Escalate ticket 5 because the customer is threatening to churn", action="escalate_ticket", key=("ticket_id", 5)),
        dict(user="ananya", q=f"Remind me tomorrow to call {f['name']} about the security questionnaire", action="create_task", key=("account", f["name"])),
    ]
    for w in workflow:
        w["kind"], w["intent"] = "workflow", "workflow"
    other = [
        dict(kind="scope", intent="out_of_scope", user="ananya", q="What's the weather in Paris tomorrow?"),
        dict(kind="scope", intent="out_of_scope", user="lena", q="Write me a poem about the ocean"),
        dict(kind="scope", intent="chitchat", user="vikram", q="Hi! What can you help me with?"),
        dict(kind="scope", intent="chitchat", user="ananya", q="Thanks, that's all"),
    ]
    multi = [
        dict(kind="multi", user="ananya", turns=[f"Tell me about {f['name']}", "What about their open tickets?", "And who is their primary contact?"],
             checks=[None, {"tool": "list_tickets|get_account", "account": f["name"], "expect": [[str(f["open_tickets"])]] if f["open_tickets"] else [["no"]]},
                     {"expect": [[f["primary"]]]}]),
        dict(kind="multi", user="ananya", turns=[f"What is the ARR of {f2['name']}?", "How many open deals do they have?"],
             checks=[None, {"tool": "list_deals|get_account", "account": f2["name"], "expect": [[str(f2["open_deals"])]] if f2["open_deals"] else [["no"]]}]),
    ]
    return crm + k + rbac + workflow + other + multi


# ---------------------------------------------------------------- runner
def run_one(user: str, q: str, conv: int | None = None):
    with gpu_lock("crm-eval"):
        cid = conv or new_conversation(user).id
        t0 = time.perf_counter()
        m = ask(user, cid, q)
        return m, cid, (time.perf_counter() - t0) * 1000


def tool_ok(m, spec: str, account: str | None = None) -> bool:  # noqa: ANN001
    tools = [t for t in m.tool_calls if t.get("ok")]
    if not any(t["tool"] in spec.split("|") for t in tools):
        return False
    if account:
        return any(str(t["args"].get("account", "")).lower() == account.lower() for t in tools)
    return True


def score_expect(text: str, expect: list[list[str]] | None) -> bool:
    if expect is None:
        return False
    return all(has_any(text, alts) for alts in expect)


def results_of(m) -> list:  # noqa: ANN001
    return [t.get("result", {}) for t in m.tool_calls if t.get("ok")]


def evaluate(cases: list[dict]) -> list[dict]:
    rows = []
    for c in cases:
        if c["kind"] == "multi":
            cid = None
            for i, (turn, chk) in enumerate(zip(c["turns"], c["checks"])):
                m, cid, ms = run_one(c["user"], turn, cid)
                if chk is None:
                    continue
                ok = score_expect(m.content, chk.get("expect")) and (not chk.get("tool") or tool_ok(m, chk["tool"], chk.get("account")))
                rows.append({"kind": "multi", "user": c["user"], "q": turn, "turn": i + 1, "standalone": m.standalone_query, "answer": m.content,
                             "intent": m.intent, "expected_intent": "crm_data", "ok": ok, "ms": ms, "llm_calls": m.llm_calls,
                             "tools": [(t["tool"], t["args"]) for t in m.tool_calls]})
            continue
        m, _, ms = run_one(c["user"], c["q"])
        row = {"kind": c["kind"], "user": c["user"], "q": c["q"], "answer": m.content, "intent": m.intent,
               "expected_intent": c["intent"], "ms": ms, "llm_calls": m.llm_calls, "tools": [(t["tool"], t["args"], t.get("kind")) for t in m.tool_calls]}
        if c["kind"] == "crm":
            row["tool_ok"] = tool_ok(m, c["tool"])
            row["ok"] = score_expect(m.content, c["expect"])
            v = validate_answer(m.content, results_of(m), c["q"])
            row["unsupported_values"] = v["issues"]
            row["validation"] = m.validation
        elif c["kind"] == "knowledge":
            row["not_found"] = bool((m.validation or {}).get("not_found"))
            row["ok"] = row["not_found"] if c["expect"] is None else score_expect(m.content, c["expect"])
            row["unanswerable"] = c["expect"] is None
        elif c["kind"] == "rbac":
            leaked = has_any(m.content, c["forbid"])
            row["ok"] = not leaked
            row["leaked"] = leaked
        elif c["kind"] == "workflow":
            pa = m.pending_action or {}
            k, val = c["key"]
            row["ok"] = pa.get("action") == c["action"] and str(pa.get("args", {}).get(k, "")).lower() == str(val).lower()
            row["pending"] = pa
        else:
            row["ok"] = m.intent == c["intent"]
        rows.append(row)
        log.info("%-9s %-6s ok=%s intent=%s %.0fms %s", c["kind"], c["user"], row.get("ok"), m.intent, ms, c["q"][:60])
    return rows


def summarise(rows: list[dict]) -> dict:
    def acc(kind: str) -> dict:
        r = [x for x in rows if x["kind"] == kind]
        return {"accuracy": sum(x["ok"] for x in r) / len(r) if r else None, "n": len(r)}

    crm = [x for x in rows if x["kind"] == "crm"]
    lat_by: dict[str, list[float]] = {}
    for x in rows:
        lat_by.setdefault(x["intent"], []).append(x["ms"])
    intents = [x for x in rows if x.get("expected_intent")]
    return {
        "intent_accuracy": sum(x["intent"] == x["expected_intent"] for x in intents) / len(intents), "intent_n": len(intents),
        "tool_selection_accuracy": sum(x["tool_ok"] for x in crm) / len(crm),
        "crm_answer_accuracy": acc("crm"), "multi_turn_accuracy": acc("multi"), "knowledge_accuracy": acc("knowledge"),
        "rbac": {"leaks": sum(x.get("leaked", False) for x in rows), **acc("rbac")},
        "workflow_accuracy": acc("workflow"), "scope_accuracy": acc("scope"),
        "crm_answers_with_unsupported_values": sum(bool(x["unsupported_values"]) for x in crm),
        "crm_first_drafts_failed_validation": sum(bool((x.get("validation") or {}).get("first_issues")) for x in crm),
        "latency_ms": {k: {"p50": round(statistics.median(v)), "max": round(max(v))} for k, v in lat_by.items()},
        "latency_p50_ms": round(statistics.median(x["ms"] for x in rows)),
        "llm_calls_avg": round(statistics.mean(x["llm_calls"] for x in rows), 2), "n": len(rows),
    }


def import_result(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    rel = path.relative_to(PROJECT_ROOT).as_posix()
    with session() as db:
        if db.query(EvalRun).filter(EvalRun.results_file == rel).first():
            return
        db.add(EvalRun(release=data["release"], label=data["label"], config=data.get("config", {}), metrics=data["metrics"],
                       n_questions=data["metrics"].get("with_validation", {}).get("n", 0), hardware=data.get("hardware", ""),
                       results_file=rel))
        db.commit()


def hardware() -> str:
    try:
        import torch

        gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "no GPU"
    except Exception:
        gpu = "unknown GPU"
    s = get_settings()
    return f"{gpu}, {platform.processor() or platform.machine()}, {platform.system()} {platform.release()}; LLM {s.llm_model} (Ollama)"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", default=get_settings().release)
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--skip-ablation", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    for noisy in ("httpx", "sentence_transformers", "crm.retry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    seed_all_crm()
    T = truth()
    cases = build_cases(T)
    s = get_settings()
    s.validate_answers = True
    rows = evaluate(cases)
    metrics = {"with_validation": summarise(rows)}
    ablation_rows = []
    if not args.skip_ablation:
        s.validate_answers = False
        ablation_rows = evaluate([c for c in cases if c["kind"] == "crm"])
        s.validate_answers = True
        crm_off = ablation_rows
        metrics["without_validation"] = {"crm_answer_accuracy": sum(x["ok"] for x in crm_off) / len(crm_off),
                                         "crm_answers_with_unsupported_values": sum(bool(x["unsupported_values"]) for x in crm_off),
                                         "n": len(crm_off)}
    out = {"release": args.release, "label": args.label, "created_at": datetime.now(timezone.utc).isoformat(), "hardware": hardware(),
           "config": {"llm": s.llm_model, "embed": s.embed_model, "reranker": s.reranker_model}, "metrics": metrics,
           "details": {"rows": rows, "ablation_rows": ablation_rows, "truth": json.loads(json.dumps(T, default=str))}}
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f"{args.release}_{args.label.replace(' ', '-')}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    import_result(path)
    print(json.dumps(metrics, indent=1))
    print(f"saved {path}")


if __name__ == "__main__":
    main()



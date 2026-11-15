"""Access-controlled CRM service layer. The REST API and the assistant's tools both go through these functions, always
as the signed-in user, so the assistant can never return a record the user could not open in the CRM itself.

Access rules (row level + field level):
* sales_rep      - own accounts, own deals/activities, tickets on own accounts
* sales_manager  - every record owned by a rep in the manager's team (region)
* support_agent  - all accounts (no revenue fields), all tickets; NO deals, pipeline or revenue
* admin          - everything
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..db import Account, Activity, AuditLog, Contact, Deal, Ticket, User, session

OPEN_STAGES = ("Prospecting", "Qualification", "Proposal", "Negotiation")
ALL_STAGES = OPEN_STAGES + ("Closed Won", "Closed Lost")
REVENUE_FIELDS = ("arr",)


class PermissionDenied(Exception):
    pass


class NotFound(Exception):
    def __init__(self, message: str, candidates: list[str] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    crm_role: str
    team: str

    @classmethod
    def from_user(cls, u: User) -> "Principal":
        return cls(u.id, u.name, u.crm_role, u.team)

    @property
    def sees_revenue(self) -> bool:
        return self.crm_role in ("sales_rep", "sales_manager", "admin")


def today() -> date:
    return date.today()


def _team_reps(db, team: str) -> list[str]:  # noqa: ANN001
    return [u.id for u in db.query(User).filter(User.team == team, User.crm_role == "sales_rep").all()]


def _account_scope(db, p: Principal):  # noqa: ANN001, ANN202
    q = db.query(Account)
    if p.crm_role == "sales_rep":
        q = q.filter(Account.owner_id == p.id)
    elif p.crm_role == "sales_manager":
        q = q.filter(Account.owner_id.in_(_team_reps(db, p.team)))
    return q


def _require_revenue(p: Principal) -> None:
    if not p.sees_revenue:
        raise PermissionDenied("Your role (support) cannot access deals, pipeline or revenue data.")


def _owner_names(db) -> dict[str, str]:  # noqa: ANN001
    return {u.id: u.name for u in db.query(User).all()}


def _account_row(a: Account, p: Principal, owners: dict[str, str]) -> dict[str, Any]:
    d = {"id": a.id, "name": a.name, "industry": a.industry, "region": a.region, "tier": a.tier,
         "owner": owners.get(a.owner_id, a.owner_id), "health_score": a.health_score,
         "renewal_date": a.renewal_date.isoformat() if a.renewal_date else None}
    if p.sees_revenue:
        d["arr"] = a.arr
    return d


def resolve_account(db, p: Principal, ref: str | int) -> Account:  # noqa: ANN001
    """Find one visible account by id or (fuzzy) name. Raises NotFound with suggestions when ambiguous or missing."""
    q = _account_scope(db, p)
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        a = q.filter(Account.id == int(ref)).first()
        if a is None:
            raise NotFound(f"No account with id {ref} that you can access.")
        return a
    name = str(ref).strip().lower()
    visible = q.all()
    exact = [a for a in visible if a.name.lower() == name]
    if exact:
        return exact[0]
    contains = [a for a in visible if name in a.name.lower()]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        raise NotFound(f"'{ref}' matches several accounts.", [a.name for a in contains[:6]])
    close = difflib.get_close_matches(name, [a.name.lower() for a in visible], n=3, cutoff=0.6)
    if len(close) == 1:
        return next(a for a in visible if a.name.lower() == close[0])
    hidden = db.query(Account).filter(Account.name.ilike(f"%{name}%")).first()
    if hidden is not None:
        raise PermissionDenied(f"You do not have access to the account '{hidden.name}'.")
    raise NotFound(f"No account named '{ref}' that you can access.",
                   [a.name for a in visible if a.name.lower() in close])


# ---------------------------------------------------------------- read tools
def search_accounts(p: Principal, query: str | None = None, industry: str | None = None, region: str | None = None,
                    tier: str | None = None, max_health: int | None = None, renewal_within_days: int | None = None,
                    limit: int = 15) -> dict:
    with session() as db:
        q = _account_scope(db, p)
        if query:
            q = q.filter(Account.name.ilike(f"%{query}%"))
        if industry:
            q = q.filter(Account.industry.ilike(f"%{industry}%"))
        if region:
            q = q.filter(Account.region.ilike(region))
        if tier:
            q = q.filter(Account.tier.ilike(f"%{tier}%"))
        if max_health is not None:
            q = q.filter(Account.health_score <= max_health)
        if renewal_within_days is not None:
            q = q.filter(Account.renewal_date >= today(), Account.renewal_date <= today() + timedelta(days=renewal_within_days))
        rows = q.order_by(Account.name).all()
        owners = _owner_names(db)
        out = [_account_row(a, p, owners) for a in rows]
    res = {"count": len(out), "accounts": out[:limit]}
    if p.sees_revenue:
        res["total_arr"] = sum(a["arr"] for a in out)
    return res


def get_account(p: Principal, account: str | int) -> dict:
    with session() as db:
        a = resolve_account(db, p, account)
        owners = _owner_names(db)
        contacts = db.query(Contact).filter(Contact.account_id == a.id).all()
        tickets = db.query(Ticket).filter(Ticket.account_id == a.id, Ticket.status != "resolved").all()
        acts = (db.query(Activity).filter(Activity.account_id == a.id).order_by(Activity.created_at.desc()).limit(5).all())
        out = {"account": _account_row(a, p, owners),
               "contacts": [{"name": c.name, "title": c.title, "email": c.email, "phone": c.phone, "primary": c.is_primary}
                            for c in contacts],
               "open_tickets": [_ticket_row(t, a.name) for t in tickets],
               "recent_activities": [_activity_row(x, a.name) for x in acts]}
        if p.sees_revenue:
            deals = db.query(Deal).filter(Deal.account_id == a.id, Deal.stage.in_(OPEN_STAGES)).all()
            out["open_deals"] = [_deal_row(d, a.name, owners) for d in deals]
            out["open_pipeline"] = sum(d.amount for d in deals)
        return out


def _deal_row(d: Deal, account_name: str, owners: dict[str, str]) -> dict:
    return {"id": d.id, "name": d.name, "account": account_name, "stage": d.stage, "amount": d.amount,
            "probability": d.probability, "close_date": d.close_date.isoformat(), "owner": owners.get(d.owner_id, d.owner_id)}


def list_deals(p: Principal, account: str | None = None, stage: str | None = None, open_only: bool = True,
               closing_within_days: int | None = None, min_amount: float | None = None, owner: str | None = None,
               limit: int = 20) -> dict:
    _require_revenue(p)
    with session() as db:
        accounts = {a.id: a.name for a in _account_scope(db, p).all()}
        q = db.query(Deal).filter(Deal.account_id.in_(accounts))
        if account:
            q = q.filter(Deal.account_id == resolve_account(db, p, account).id)
        if stage:
            match = [s for s in ALL_STAGES if s.lower() == stage.lower()]
            if not match:
                raise NotFound(f"Unknown stage '{stage}'.", list(ALL_STAGES))
            q = q.filter(Deal.stage == match[0])
        elif open_only:
            q = q.filter(Deal.stage.in_(OPEN_STAGES))
        if closing_within_days is not None:
            q = q.filter(Deal.close_date >= today(), Deal.close_date <= today() + timedelta(days=closing_within_days))
        if min_amount is not None:
            q = q.filter(Deal.amount >= min_amount)
        owners = _owner_names(db)
        if owner:
            ids = [uid for uid, n in owners.items() if owner.lower() in n.lower() or owner.lower() == uid]
            q = q.filter(Deal.owner_id.in_(ids))
        rows = q.order_by(Deal.close_date).all()
        out = [_deal_row(d, accounts[d.account_id], owners) for d in rows]
    return {"count": len(out), "total_amount": sum(d["amount"] for d in out),
            "weighted_amount": round(sum(d["amount"] * d["probability"] / 100 for d in out), 2), "deals": out[:limit]}


def pipeline_summary(p: Principal, group_by: str = "stage") -> dict:
    _require_revenue(p)
    with session() as db:
        accounts = {a.id for a in _account_scope(db, p).all()}
        deals = db.query(Deal).filter(Deal.account_id.in_(accounts), Deal.stage.in_(OPEN_STAGES)).all()
        owners = _owner_names(db)
    groups: dict[str, dict] = {}
    for d in deals:
        key = {"owner": owners.get(d.owner_id, d.owner_id), "month": d.close_date.strftime("%Y-%m")}.get(group_by, d.stage)
        g = groups.setdefault(key, {"group": key, "count": 0, "total_amount": 0.0, "weighted_amount": 0.0})
        g["count"] += 1
        g["total_amount"] += d.amount
        g["weighted_amount"] = round(g["weighted_amount"] + d.amount * d.probability / 100, 2)
    order = list(OPEN_STAGES) if group_by == "stage" else sorted(groups)
    rows = [groups[k] for k in order if k in groups]
    return {"group_by": group_by if group_by in ("owner", "month") else "stage", "groups": rows,
            "count": len(deals), "total_amount": sum(d.amount for d in deals),
            "weighted_amount": round(sum(d.amount * d.probability / 100 for d in deals), 2)}


def _ticket_row(t: Ticket, account_name: str) -> dict:
    now = datetime.now(timezone.utc)
    sla = t.sla_due if t.sla_due.tzinfo else t.sla_due.replace(tzinfo=timezone.utc)
    return {"id": t.id, "account": account_name, "subject": t.subject, "priority": t.priority, "status": t.status,
            "escalated": t.escalated, "sla_due": sla.isoformat(), "sla_breached": t.status != "resolved" and sla < now}


def list_tickets(p: Principal, account: str | None = None, status: str | None = None, priority: str | list[str] | None = None,
                 sla_breached_only: bool = False, limit: int = 20) -> dict:
    with session() as db:
        accounts = {a.id: a.name for a in _account_scope(db, p).all()} if p.crm_role != "support_agent" \
            else {a.id: a.name for a in db.query(Account).all()}
        q = db.query(Ticket).filter(Ticket.account_id.in_(accounts))
        if account:
            acc = resolve_account(db, p, account) if p.crm_role != "support_agent" else \
                resolve_account(db, Principal(p.id, p.name, "admin", ""), account)
            q = q.filter(Ticket.account_id == acc.id)
        if status in (None, "", "open"):
            q = q.filter(Ticket.status.in_(["open", "pending"]))
        elif status != "all":
            q = q.filter(Ticket.status == status)
        if priority:
            prios = [priority] if isinstance(priority, str) else list(priority)
            q = q.filter(Ticket.priority.in_([x.upper() for x in prios]))
        rows = [_ticket_row(t, accounts[t.account_id]) for t in q.order_by(Ticket.priority, Ticket.sla_due).all()]
    if sla_breached_only:
        rows = [r for r in rows if r["sla_breached"]]
    return {"count": len(rows), "sla_breached": sum(r["sla_breached"] for r in rows), "tickets": rows[:limit]}


def _activity_row(a: Activity, account_name: str) -> dict:
    return {"id": a.id, "account": account_name, "kind": a.kind, "subject": a.subject,
            "due_date": a.due_date.isoformat() if a.due_date else None, "done": a.done,
            "overdue": bool(a.due_date and not a.done and a.due_date < today())}


def list_activities(p: Principal, account: str | None = None, kind: str | None = None, open_tasks_only: bool = False,
                    overdue_only: bool = False, due_within_days: int | None = None, limit: int = 20) -> dict:
    _require_revenue(p)
    with session() as db:
        accounts = {a.id: a.name for a in _account_scope(db, p).all()}
        q = db.query(Activity).filter(Activity.account_id.in_(accounts))
        if account:
            q = q.filter(Activity.account_id == resolve_account(db, p, account).id)
        if kind:
            q = q.filter(Activity.kind == kind.lower())
        if open_tasks_only or overdue_only or due_within_days is not None:
            q = q.filter(Activity.kind == "task", Activity.done.is_(False))
        if overdue_only:
            q = q.filter(Activity.due_date < today())
        if due_within_days is not None:
            q = q.filter(Activity.due_date >= today(), Activity.due_date <= today() + timedelta(days=due_within_days))
        rows = [_activity_row(a, accounts[a.account_id]) for a in q.order_by(Activity.due_date, Activity.created_at.desc()).all()]
    return {"count": len(rows), "activities": rows[:limit]}


# ---------------------------------------------------------------- write actions (run only after user confirmation)
def _audit(db, p: Principal, action: str, target: str, details: dict) -> None:  # noqa: ANN001
    db.add(AuditLog(user_id=p.id, action=action, target=target, details=details))


def create_task(p: Principal, account: str, subject: str, due_in_days: int = 3) -> dict:
    _require_revenue(p)
    with session() as db:
        a = resolve_account(db, p, account)
        t = Activity(account_id=a.id, kind="task", subject=subject[:300], due_date=today() + timedelta(days=int(due_in_days)),
                     done=False, owner_id=p.id)
        db.add(t)
        db.flush()
        _audit(db, p, "create_task", a.name, {"activity_id": t.id, "subject": subject, "due_in_days": due_in_days})
        db.commit()
        return {"created": "task", "id": t.id, "account": a.name, "subject": t.subject, "due_date": t.due_date.isoformat()}


def log_note(p: Principal, account: str, text: str) -> dict:
    with session() as db:
        scope_p = p if p.crm_role != "support_agent" else Principal(p.id, p.name, "admin", "")
        a = resolve_account(db, scope_p, account)
        n = Activity(account_id=a.id, kind="note", subject=text[:1000], done=True, owner_id=p.id)
        db.add(n)
        db.flush()
        _audit(db, p, "log_note", a.name, {"activity_id": n.id})
        db.commit()
        return {"created": "note", "id": n.id, "account": a.name, "text": n.subject}


def update_deal_stage(p: Principal, deal: str | int, stage: str) -> dict:
    _require_revenue(p)
    match = [s for s in ALL_STAGES if s.lower() == str(stage).lower()]
    if not match:
        raise NotFound(f"Unknown stage '{stage}'.", list(ALL_STAGES))
    with session() as db:
        accounts = {a.id: a.name for a in _account_scope(db, p).all()}
        q = db.query(Deal).filter(Deal.account_id.in_(accounts))
        if isinstance(deal, int) or str(deal).isdigit():
            d = q.filter(Deal.id == int(deal)).first()
        else:
            cands = [x for x in q.all() if str(deal).lower() in x.name.lower()]
            if len(cands) > 1:
                raise NotFound(f"'{deal}' matches several deals.", [x.name for x in cands[:6]])
            d = cands[0] if cands else None
        if d is None:
            raise NotFound(f"No deal '{deal}' that you can access.")
        old = d.stage
        d.stage = match[0]
        d.probability = dict(zip(ALL_STAGES, (10, 25, 50, 75, 100, 0)))[match[0]]
        d.updated_at = datetime.now(timezone.utc)
        _audit(db, p, "update_deal_stage", d.name, {"deal_id": d.id, "from": old, "to": d.stage})
        db.commit()
        return {"updated": "deal", "id": d.id, "deal": d.name, "from": old, "to": d.stage}


def escalate_ticket(p: Principal, ticket_id: int, reason: str = "") -> dict:
    if p.crm_role not in ("support_agent", "admin", "sales_manager"):
        raise PermissionDenied("Only support agents and managers can escalate tickets.")
    with session() as db:
        t = db.get(Ticket, int(ticket_id))
        visible = {a.id for a in (_account_scope(db, p).all() if p.crm_role == "sales_manager" else db.query(Account).all())}
        if t is None or t.account_id not in visible:
            raise NotFound(f"No ticket #{ticket_id} that you can access.")
        t.escalated = True
        if t.priority in ("P3", "P4"):
            t.priority = "P2"
        _audit(db, p, "escalate_ticket", f"ticket #{t.id}", {"reason": reason, "priority": t.priority})
        db.commit()
        return {"escalated": t.id, "subject": t.subject, "priority": t.priority}

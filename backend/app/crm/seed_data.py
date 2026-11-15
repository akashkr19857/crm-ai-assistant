"""Deterministic, clearly synthetic CRM data for the demo (fixed random seed, dates relative to today).

All companies, people, emails and phone numbers are invented ("example.com" domains, 555 numbers).
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone

from ..db import Account, Activity, Contact, Deal, Ticket, User, Workspace, session

# (id, name, title, crm_role, team, token, workspaces)
USERS = [
    ("ananya", "Ananya Rao", "Account Executive, West", "sales_rep", "West", "demo-ananya", ["product-docs", "sales-playbook"]),
    ("vikram", "Vikram Mehta", "Sales Manager, West", "sales_manager", "West", "demo-vikram", ["product-docs", "sales-playbook"]),
    ("lena", "Lena Ortiz", "Customer Support Agent", "support_agent", "", "demo-lena", ["product-docs"]),
    ("admin", "Riya Kapoor", "RevOps Admin", "admin", "", "demo-admin", ["product-docs", "sales-playbook"]),
    # reps who own records but are not demo logins
    ("james", "James Carter", "Account Executive, West", "sales_rep", "West", "demo-james", ["product-docs", "sales-playbook"]),
    ("sofia", "Sofia Lindqvist", "Account Executive, East", "sales_rep", "East", "demo-sofia", ["product-docs", "sales-playbook"]),
    ("marcus", "Marcus Bell", "Account Executive, East", "sales_rep", "East", "demo-marcus", ["product-docs", "sales-playbook"]),
]
LOGIN_USERS = ["ananya", "vikram", "lena", "admin"]
WORKSPACES = [("product-docs", "Product docs", "NimbusCRM help center and product guides"),
              ("sales-playbook", "Sales playbook", "Pricing, discount approval and sales process (sales roles only)")]
SUPPORT_AGENTS = ["lena"]

PREFIX = ["Apex", "Blue Harbor", "Cedar", "Northwind", "Silverline", "Brightpath", "Ironwood", "Lumen", "Maple Ridge",
          "Orbit", "Pinecrest", "Quantum Leaf", "Redstone", "Summit", "Tidewater", "Vertex", "Willow Creek", "Zenith",
          "Coral Bay", "Granite Peak", "Horizon", "Kestrel", "Meridian", "Nova", "Oakline", "Prairie", "Riverstone",
          "Starling", "Trident", "Union Square", "Evergreen", "Falcon", "Golden Gate", "Harbor Point", "Juniper",
          "Lakeshore", "Mosaic", "Northstar", "Paragon", "Sequoia"]
SUFFIX = ["Logistics", "Health", "Retail", "Manufacturing", "Financial", "Energy", "Foods", "Software", "Labs", "Hotels"]
INDUSTRY = {"Logistics": "Logistics", "Health": "Healthcare", "Retail": "Retail", "Manufacturing": "Manufacturing",
            "Financial": "Financial Services", "Energy": "Energy", "Foods": "Food & Beverage", "Software": "Technology",
            "Labs": "Life Sciences", "Hotels": "Hospitality"}
FIRST = ["Aarav", "Maya", "Daniel", "Priya", "Lucas", "Emma", "Kenji", "Fatima", "Noah", "Chloe", "Rahul", "Olivia",
         "Mateo", "Sara", "Ethan", "Zara", "Leo", "Isha", "Owen", "Nina"]
LAST = ["Shah", "Johnson", "Kim", "Garcia", "Patel", "Novak", "Okafor", "Silva", "Brown", "Ito", "Fischer", "Nair",
        "Martin", "Rossi", "Haddad", "Clark", "Iyer", "Lopez", "Walsh", "Chen"]
TITLES = ["VP Operations", "CIO", "Head of Sales Ops", "IT Director", "CFO", "Procurement Manager", "COO", "CTO"]
STAGES = [("Prospecting", 10), ("Qualification", 25), ("Proposal", 50), ("Negotiation", 75), ("Closed Won", 100),
          ("Closed Lost", 0)]
PRODUCTS = ["NimbusCRM Enterprise", "Service Cloud add-on", "Analytics Pro", "Marketing Automation", "API Platform",
            "Premium Support"]
TICKET_SUBJECTS = ["Email sync failing for shared mailbox", "SSO login loop after password reset", "Report export times out",
                   "Duplicate contacts after CSV import", "Webhook deliveries delayed", "Mobile app crashes on dashboard",
                   "Custom field missing in API response", "Invoice shows wrong currency", "Workflow rule not triggering",
                   "Slow search on large accounts"]
ACTIVITY_SUBJECTS = {"call": ["Discovery call", "Pricing discussion", "Renewal check-in", "Technical deep dive"],
                     "email": ["Sent proposal", "Shared case study", "Follow-up on security questionnaire"],
                     "meeting": ["Executive sponsor meeting", "Product demo", "QBR"],
                     "task": ["Send revised quote", "Prepare ROI analysis", "Schedule onboarding kickoff", "Confirm legal review"],
                     "note": ["Champion is moving to a new role", "Budget approved for Q1", "Competitor evaluation in progress"]}
SLA_HOURS = {"P1": 4, "P2": 8, "P3": 24, "P4": 72}


def seed_users() -> None:
    with session() as db:
        for wid, name, desc in WORKSPACES:
            if db.get(Workspace, wid) is None:
                db.add(Workspace(id=wid, name=name, description=desc))
        db.flush()
        for uid, name, title, crm_role, team, token, wss in USERS:
            if db.get(User, uid) is None:
                u = User(id=uid, name=name, token=token, role="admin" if crm_role == "admin" else "member",
                         crm_role=crm_role, title=title, team=team)
                u.workspaces = [db.get(Workspace, w) for w in wss]
                db.add(u)
        db.commit()


def seed_crm(seed: int = 7, today: date | None = None, reset: bool = False) -> dict[str, int]:
    """Create the demo CRM (idempotent unless reset=True)."""
    seed_users()
    today = today or date.today()
    rng = random.Random(seed)
    with session() as db:
        if db.query(Account).count() and not reset:
            return {"accounts": db.query(Account).count()}
        for model in (Activity, Ticket, Deal, Contact, Account):
            db.query(model).delete()
        db.commit()
        reps = {"West": ["ananya", "james"], "East": ["sofia", "marcus"]}
        names = rng.sample([f"{p} {s}" for p in PREFIX for s in SUFFIX], 40)
        for i, name in enumerate(names):
            region = "West" if i % 2 == 0 else "East"
            owner = reps[region][(i // 2) % 2]
            tier = rng.choices(["Enterprise", "Mid-Market", "SMB"], weights=[3, 4, 3])[0]
            arr = {"Enterprise": rng.randrange(250, 1200), "Mid-Market": rng.randrange(60, 250), "SMB": rng.randrange(8, 60)}[tier] * 1000
            acc = Account(name=name, industry=INDUSTRY[name.split()[-1]], region=region, tier=tier, owner_id=owner, arr=float(arr),
                          health_score=rng.randrange(35, 98), renewal_date=today + timedelta(days=rng.randrange(-20, 330)))
            db.add(acc)
            db.flush()
            domain = name.lower().replace(" ", "") + ".example.com"
            for c in range(rng.randrange(1, 4)):
                fn, ln = rng.choice(FIRST), rng.choice(LAST)
                db.add(Contact(account_id=acc.id, name=f"{fn} {ln}", title=rng.choice(TITLES), email=f"{fn.lower()}.{ln.lower()}@{domain}",
                               phone=f"+1-555-{rng.randrange(100, 999)}-{rng.randrange(1000, 9999)}", is_primary=c == 0))
            deals = []
            for _ in range(rng.choices([0, 1, 2, 3], weights=[1, 4, 3, 1])[0]):
                stage, prob = rng.choices(STAGES, weights=[3, 4, 4, 3, 3, 2])[0]
                closed = stage.startswith("Closed")
                close = today + timedelta(days=rng.randrange(-120, -5) if closed else rng.randrange(-10, 150))
                amount = round(arr * rng.uniform(0.15, 0.9) / 1000) * 1000
                d = Deal(account_id=acc.id, name=f"{name} - {rng.choice(PRODUCTS)}", stage=stage, amount=float(max(amount, 5000)),
                         probability=prob, close_date=close, owner_id=owner)
                db.add(d)
                db.flush()
                deals.append(d)
            for _ in range(rng.randrange(2, 7)):
                kind = rng.choice(list(ACTIVITY_SUBJECTS))
                due = today + timedelta(days=rng.randrange(-15, 21)) if kind == "task" else None
                deal = rng.choice(deals) if deals and rng.random() < 0.6 else None
                db.add(Activity(account_id=acc.id, deal_id=deal.id if deal else None, kind=kind,
                                subject=rng.choice(ACTIVITY_SUBJECTS[kind]), due_date=due,
                                done=(due is not None and due < today and rng.random() < 0.5) or (kind != "task"),
                                owner_id=owner,
                                created_at=datetime.combine(today - timedelta(days=rng.randrange(0, 60)), time(10), tzinfo=timezone.utc)))
            for _ in range(rng.choices([0, 1, 2, 3], weights=[3, 4, 2, 1])[0]):
                pri = rng.choices(["P1", "P2", "P3", "P4"], weights=[1, 3, 4, 2])[0]
                status = rng.choices(["open", "pending", "resolved"], weights=[4, 2, 4])[0]
                # resolved tickets can be old; open ones are recent, so roughly half are still inside their SLA window
                age = rng.randrange(1, 24 * 20) if status == "resolved" else rng.randrange(1, SLA_HOURS[pri] * 2 + 2)
                created = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=age)
                db.add(Ticket(account_id=acc.id, subject=rng.choice(TICKET_SUBJECTS), priority=pri, status=status,
                              assignee_id=rng.choice(SUPPORT_AGENTS), created_at=created,
                              sla_due=created + timedelta(hours=SLA_HOURS[pri])))
        db.commit()
        return {"accounts": db.query(Account).count(), "contacts": db.query(Contact).count(), "deals": db.query(Deal).count(),
                "activities": db.query(Activity).count(), "tickets": db.query(Ticket).count()}

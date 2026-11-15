"""SQLite persistence (SQLAlchemy 2.x): users and knowledge documents, the demo CRM, conversations, evaluation runs."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Table, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import get_settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


user_workspaces = Table(
    "user_workspaces",
    Base.metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("workspace_id", ForeignKey("workspaces.id"), primary_key=True),
)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(16), default="member")  # admin | member
    crm_role: Mapped[str] = mapped_column(String(32), default="sales_rep")  # sales_rep | sales_manager | support_agent | admin
    title: Mapped[str] = mapped_column(String(128), default="")
    team: Mapped[str] = mapped_column(String(32), default="")  # region the user's team covers
    workspaces: Mapped[list[Workspace]] = relationship(secondary=user_workspaces, lazy="selectin")

    @property
    def workspace_ids(self) -> list[str]:
        return sorted(w.id for w in self.workspaces)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(256))
    filename: Mapped[str] = mapped_column(String(256))
    path: Mapped[str] = mapped_column(Text)  # relative to project root
    source: Mapped[str] = mapped_column(Text, default="")
    file_type: Mapped[str] = mapped_column(String(8), default="pdf")  # pdf | docx
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|processing|ready|failed
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    pages: Mapped[int] = mapped_column(Integer, default=0)
    n_chunks: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    tables: Mapped[int] = mapped_column(Integer, default=0)
    ingest_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "id", "workspace_id", "title", "filename", "source", "file_type", "status", "progress", "pages", "n_chunks",
            "ocr_pages", "tables", "ingest_seconds", "error")} | {"created_at": self.created_at.isoformat()}


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    page: Mapped[int] = mapped_column(Integer)  # 1-based
    section: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="text")  # text | table | ocr
    text: Mapped[str] = mapped_column(Text)
    bboxes: Mapped[list] = mapped_column(JSON, default=list)  # [[x0,y0,x1,y1], ...] in PDF points


class EvalRun(Base):
    __tablename__ = "eval_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(128), default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    n_questions: Mapped[int] = mapped_column(Integer, default=0)
    hardware: Mapped[str] = mapped_column(Text, default="")
    results_file: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        d = {c.name: getattr(self, c.name) for c in self.__table__.columns}
        d["created_at"] = self.created_at.isoformat()
        return d


def _row(obj: Any) -> dict[str, Any]:
    d = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        d[c.name] = v.isoformat() if isinstance(v, (datetime, date)) else v
    return d


# ---------------------------------------------------------------- CRM (demo data, see crm/seed_data.py)
class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    industry: Mapped[str] = mapped_column(String(64))
    region: Mapped[str] = mapped_column(String(32))  # West | East
    tier: Mapped[str] = mapped_column(String(16))  # Enterprise | Mid-Market | SMB
    owner_id: Mapped[str] = mapped_column(String(64), index=True)  # sales rep (user id)
    arr: Mapped[float] = mapped_column(Float, default=0.0)  # annual recurring revenue, USD
    health_score: Mapped[int] = mapped_column(Integer, default=70)  # 0-100
    renewal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return _row(self)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(128))
    phone: Mapped[str] = mapped_column(String(32))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)

    def to_dict(self) -> dict[str, Any]:
        return _row(self)


class Deal(Base):
    __tablename__ = "deals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    stage: Mapped[str] = mapped_column(String(32), index=True)
    amount: Mapped[float] = mapped_column(Float)
    probability: Mapped[int] = mapped_column(Integer)
    close_date: Mapped[date] = mapped_column(Date)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return _row(self)


class Activity(Base):
    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # call | email | meeting | note | task
    subject: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return _row(self)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    subject: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(4))  # P1..P4
    status: Mapped[str] = mapped_column(String(16), index=True)  # open | pending | resolved
    assignee_id: Mapped[str] = mapped_column(String(64))
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    sla_due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return _row(self)


class AuditLog(Base):
    """Every write the assistant performs (after the user confirmed it)."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return _row(self)


# ---------------------------------------------------------------- assistant conversations
class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    memory: Mapped[dict] = mapped_column(JSON, default=dict)  # entity memory: last account / deal / ticket in focus
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return _row(self)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(32), default="")
    route: Mapped[str] = mapped_column(String(32), default="")
    standalone_query: Mapped[str] = mapped_column(Text, default="")
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)  # [{tool, args, ok, rows, ms, error}]
    sources: Mapped[list] = mapped_column(JSON, default=list)  # knowledge citations
    pending_action: Mapped[dict] = mapped_column(JSON, default=dict)  # workflow awaiting confirmation
    validation: Mapped[dict] = mapped_column(JSON, default=dict)  # {checked, passed, issues, regenerated, fallback}
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    def to_dict(self) -> dict[str, Any]:
        return _row(self)


_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        s = get_settings()
        _engine = create_engine(f"sqlite:///{s.db_path.as_posix()}", connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _pragma(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        Base.metadata.create_all(_engine)
        _SessionLocal = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def session() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def reset_engine() -> None:
    """Used by tests to point the app at a fresh temp directory."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine, _SessionLocal = None, None


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)

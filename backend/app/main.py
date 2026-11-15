"""CRM AI Assistant FastAPI app (port 8300): assistant chat (REST + WebSocket), CRM API, knowledge documents, analytics."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import statistics
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import pymupdf
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import __version__
from .config import PROJECT_ROOT, get_settings
from .crm import service as crm
from .crm.seed_data import LOGIN_USERS
from .db import AuditLog, Chunk, Conversation, Document, EvalRun, Message, User, Workspace, session
from .events import bus
from .ingest.pipeline import SUPPORTED, delete_document, ingest_document, register_document, resolve_path

log = logging.getLogger("crm")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _import_eval_history() -> None:
    results = PROJECT_ROOT / "backend" / "eval" / "results"
    if not results.exists():
        return
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from eval.run_eval import import_result

    for f in sorted(results.glob("*.json")):
        try:
            import_result(f)
        except Exception:
            log.exception("could not import eval result %s", f.name)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from .seed import seed_all_crm, seed_identities

    seed_identities()
    seed_all_crm()
    _import_eval_history()
    if get_settings().rerank_provider == "cross-encoder":
        from .retrieval.reranker import get_reranker

        threading.Thread(target=get_reranker, daemon=True).start()
    yield
    from .retrieval.hybrid import close_store

    close_store()


app = FastAPI(title="CRM AI Assistant API", version=__version__, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins.split(","), allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


# ------------------------------------------------------------------ auth
def user_from_token(token: str | None) -> User:
    if not token:
        raise HTTPException(401, "missing token")
    with session() as db:
        user = db.query(User).filter(User.token == token).first()
    if user is None:
        raise HTTPException(401, "invalid token")
    return user


def current_user(authorization: str | None = Header(default=None)) -> User:
    token = authorization.split(" ", 1)[1] if authorization and authorization.lower().startswith("bearer ") else authorization
    return user_from_token(token)


def principal(user: User = Depends(current_user)) -> crm.Principal:
    return crm.Principal.from_user(user)


def user_dict(u: User) -> dict:
    return {"id": u.id, "name": u.name, "title": u.title, "crm_role": u.crm_role, "team": u.team, "role": u.role,
            "workspaces": u.workspace_ids}


class LoginIn(BaseModel):
    user_id: str


@app.post("/api/login")
def login(body: LoginIn) -> dict:
    """Demo login: pick one of the seeded demo users (no passwords in this demo)."""
    with session() as db:
        u = db.get(User, body.user_id)
    if u is None:
        raise HTTPException(404, "unknown user")
    return {"token": u.token, "user": user_dict(u)}


@app.get("/api/users")
def demo_users() -> list[dict]:
    with session() as db:
        return [user_dict(db.get(User, uid)) for uid in LOGIN_USERS if db.get(User, uid)]


@app.get("/api/me")
def me(user: User = Depends(current_user)) -> dict:
    return user_dict(user)


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    ollama = "down"
    try:
        import httpx

        names = {m["name"] for m in httpx.get(f"{s.ollama_url}/api/tags", timeout=3).json().get("models", [])}
        ollama = "ok" if any(n.startswith(s.llm_model) for n in names) else "model missing"
    except Exception:
        pass
    return {"status": "ok", "version": __version__, "llm": s.llm_model, "embed": s.embed_model, "reranker": s.reranker_model,
            "ollama": ollama}


# ------------------------------------------------------------------ assistant
def _own_conversation(user: User, conv_id: int) -> Conversation:
    with session() as db:
        c = db.get(Conversation, conv_id)
    if c is None or c.user_id != user.id:
        raise HTTPException(404, "conversation not found")
    return c


@app.get("/api/conversations")
def conversations(user: User = Depends(current_user)) -> list[dict]:
    with session() as db:
        rows = db.query(Conversation).filter(Conversation.user_id == user.id).order_by(Conversation.id.desc()).limit(50).all()
        return [c.to_dict() for c in rows]


@app.post("/api/conversations")
def create_conversation(user: User = Depends(current_user)) -> dict:
    from .assistant.engine import new_conversation

    return new_conversation(user.id).to_dict()


@app.get("/api/conversations/{conv_id}/messages")
def conversation_messages(conv_id: int, user: User = Depends(current_user)) -> list[dict]:
    _own_conversation(user, conv_id)
    with session() as db:
        return [m.to_dict() for m in db.query(Message).filter(Message.conversation_id == conv_id).order_by(Message.id).all()]


class AskIn(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


@app.post("/api/conversations/{conv_id}/messages")
def post_message(conv_id: int, body: AskIn, user: User = Depends(current_user)) -> dict:
    from .assistant.engine import ask

    _own_conversation(user, conv_id)
    return ask(user.id, conv_id, body.content.strip()).to_dict()


class ConfirmIn(BaseModel):
    accept: bool


@app.post("/api/messages/{message_id}/confirm")
def confirm_action(message_id: int, body: ConfirmIn, user: User = Depends(current_user)) -> dict:
    from .assistant.engine import confirm

    try:
        return confirm(user.id, message_id, body.accept).to_dict()
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.websocket("/ws/assistant")
async def ws_assistant(ws: WebSocket, token: str = Query(...)) -> None:
    """Send {"conversation_id", "content"}; receive live {"type": "step"} events, then {"type": "final", "message"}."""
    try:
        user = user_from_token(token)
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    loop = asyncio.get_running_loop()
    from .assistant.engine import ask

    try:
        while True:
            try:
                req = json.loads(await ws.receive_text())
                conv_id, content = int(req["conversation_id"]), str(req["content"]).strip()
                _own_conversation(user, conv_id)
                if not content:
                    raise ValueError("empty message")
            except (HTTPException, ValueError, KeyError, TypeError) as exc:
                await ws.send_json({"type": "error", "error": getattr(exc, "detail", str(exc))})
                continue
            queue: asyncio.Queue = asyncio.Queue()

            def emit(ev: dict) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, ev)

            def worker() -> None:
                try:
                    emit({"type": "final", "message": ask(user.id, conv_id, content, emit).to_dict()})
                except Exception as exc:  # surfaced to the UI
                    log.exception("assistant failed")
                    emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})

            threading.Thread(target=worker, daemon=True).start()
            while True:
                ev = await queue.get()
                await ws.send_json(json.loads(json.dumps(ev, default=str)))
                if ev["type"] in ("final", "error"):
                    break
    except WebSocketDisconnect:
        return


# ------------------------------------------------------------------ CRM API (same access rules as the assistant)
def _crm_call(fn, *args, **kwargs) -> dict:  # noqa: ANN001
    try:
        return fn(*args, **kwargs)
    except crm.PermissionDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    except crm.NotFound as exc:
        raise HTTPException(404, {"message": str(exc), "candidates": exc.candidates}) from exc


@app.get("/api/crm/accounts")
def api_accounts(query: str | None = None, region: str | None = None, tier: str | None = None, limit: int = 100,
                 p: crm.Principal = Depends(principal)) -> dict:
    return _crm_call(crm.search_accounts, p, query=query, region=region, tier=tier, limit=limit)


@app.get("/api/crm/accounts/{ref}")
def api_account(ref: str, p: crm.Principal = Depends(principal)) -> dict:
    return _crm_call(crm.get_account, p, ref)


@app.get("/api/crm/deals")
def api_deals(stage: str | None = None, open_only: bool = True, limit: int = 200, p: crm.Principal = Depends(principal)) -> dict:
    return _crm_call(crm.list_deals, p, stage=stage, open_only=open_only, limit=limit)


@app.get("/api/crm/pipeline")
def api_pipeline(group_by: str = "stage", p: crm.Principal = Depends(principal)) -> dict:
    return _crm_call(crm.pipeline_summary, p, group_by=group_by)


@app.get("/api/crm/tickets")
def api_tickets(status: str = "open", limit: int = 200, p: crm.Principal = Depends(principal)) -> dict:
    return _crm_call(crm.list_tickets, p, status=status, limit=limit)


@app.get("/api/crm/activities")
def api_activities(open_tasks_only: bool = False, limit: int = 200, p: crm.Principal = Depends(principal)) -> dict:
    return _crm_call(crm.list_activities, p, open_tasks_only=open_tasks_only, limit=limit)


@app.get("/api/crm/audit")
def api_audit(user: User = Depends(current_user)) -> list[dict]:
    with session() as db:
        q = db.query(AuditLog)
        if user.crm_role != "admin":
            q = q.filter(AuditLog.user_id == user.id)
        return [a.to_dict() for a in q.order_by(AuditLog.id.desc()).limit(100).all()]


# ------------------------------------------------------------------ knowledge documents
def scope(user: User, workspace: str | None) -> list[str]:
    allowed = user.workspace_ids
    if workspace in (None, "", "all"):
        return allowed
    if workspace not in allowed:
        raise HTTPException(403, f"no access to workspace '{workspace}'")
    return [workspace]


def get_doc_checked(user: User, doc_id: int) -> Document:
    with session() as db:
        doc = db.get(Document, doc_id)
    if doc is None or doc.workspace_id not in user.workspace_ids:
        raise HTTPException(404, "document not found")
    return doc


@app.get("/api/workspaces")
def workspaces(user: User = Depends(current_user)) -> list[dict]:
    with session() as db:
        return [{"id": w.id, "name": w.name, "description": w.description,
                 "documents": db.query(Document).filter(Document.workspace_id == w.id).count()}
                for w in db.query(Workspace).filter(Workspace.id.in_(user.workspace_ids)).all()]


@app.get("/api/documents")
def documents(workspace: str | None = None, user: User = Depends(current_user)) -> list[dict]:
    wss = scope(user, workspace)
    with session() as db:
        return [d.to_dict() for d in db.query(Document).filter(Document.workspace_id.in_(wss)).order_by(Document.id).all()]


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_ingest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")


@app.post("/api/documents", status_code=202)
async def upload(file: UploadFile = File(...), workspace: str = Form(...), title: str = Form(""),
                 user: User = Depends(current_user)) -> dict:
    scope(user, workspace)
    if user.crm_role not in ("admin", "sales_manager"):
        raise HTTPException(403, "only admins and managers can add knowledge documents")
    name = (file.filename or "").lower()
    if not name.endswith(SUPPORTED):
        raise HTTPException(400, "only PDF and DOCX files are supported")
    data = await file.read()
    if name.endswith(".pdf") and not data.startswith(b"%PDF"):
        raise HTTPException(400, "file is not a PDF")
    if name.endswith(".docx") and not data.startswith(b"PK"):
        raise HTTPException(400, "file is not a DOCX")
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "file larger than 50 MB")
    dest_dir = get_settings().upload_dir / workspace
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _SAFE.sub("_", Path(file.filename or "upload.pdf").name)
    i = 1
    while dest.exists():
        dest = dest.with_name(f"{dest.stem}-{i}{dest.suffix}")
        i += 1
    dest.write_bytes(data)
    doc_id = register_document(dest, workspace, title or None, source=f"uploaded by {user.id}")
    _ingest_pool.submit(ingest_document, doc_id)
    with session() as db:
        return db.get(Document, doc_id).to_dict()


@app.delete("/api/documents/{doc_id}")
def remove(doc_id: int, user: User = Depends(current_user)) -> dict:
    get_doc_checked(user, doc_id)
    if user.crm_role not in ("admin", "sales_manager"):
        raise HTTPException(403, "only admins and managers can delete knowledge documents")
    delete_document(doc_id)
    bus.publish({"type": "document_deleted", "id": doc_id})
    return {"deleted": doc_id}


@app.get("/api/documents/{doc_id}/file")
def doc_file(doc_id: int, token: str | None = None, authorization: str | None = Header(default=None)) -> FileResponse:
    user = current_user(authorization) if authorization else user_from_token(token)
    doc = get_doc_checked(user, doc_id)
    media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document" if doc.file_type == "docx"
             else "application/pdf")
    return FileResponse(resolve_path(doc), media_type=media, filename=doc.filename)


@app.get("/api/documents/{doc_id}/chunks")
def doc_chunks(doc_id: int, page: int | None = None, user: User = Depends(current_user)) -> list[dict]:
    get_doc_checked(user, doc_id)
    with session() as db:
        q = db.query(Chunk).filter(Chunk.doc_id == doc_id)
        if page:
            q = q.filter(Chunk.page == page)
        return [{"id": c.id, "page": c.page, "section": c.section, "kind": c.kind, "text": c.text}
                for c in q.order_by(Chunk.page).limit(500).all()]


@app.get("/api/documents/{doc_id}/pages/{page}.png")
def page_image(doc_id: int, page: int, chunk_id: str | None = None, token: str | None = None, zoom: float = 1.6,
               authorization: str | None = Header(default=None)) -> Response:
    user = current_user(authorization) if authorization else user_from_token(token)
    doc = get_doc_checked(user, doc_id)
    if doc.file_type != "pdf":
        raise HTTPException(415, "page images are only available for PDF documents")
    boxes: list = []
    if chunk_id:
        with session() as db:
            ch = db.get(Chunk, chunk_id)
        if ch is not None and ch.doc_id == doc_id and ch.page == page:
            boxes = ch.bboxes
    with pymupdf.open(resolve_path(doc)) as pdf:
        if not 1 <= page <= len(pdf):
            raise HTTPException(404, "page out of range")
        pg = pdf[page - 1]
        for b in boxes:
            pg.draw_rect(pymupdf.Rect(b) + (-2, -2, 2, 2), color=(0.96, 0.62, 0.04), fill=(1.0, 0.85, 0.2), fill_opacity=0.28,
                         width=1.2, overlay=True)
        png = pg.get_pixmap(matrix=pymupdf.Matrix(min(zoom, 3.0), min(zoom, 3.0))).tobytes("png")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})


@app.websocket("/ws/events")
async def ws_events(ws: WebSocket, token: str = Query(...)) -> None:
    try:
        user = user_from_token(token)
    except HTTPException:
        await ws.close(code=4401)
        return
    await ws.accept()
    q = bus.subscribe()
    allowed = set(user.workspace_ids)
    try:
        while True:
            ev = await q.get()
            if ev["type"] == "document" and ev["document"]["workspace_id"] not in allowed:
                continue
            await ws.send_json(ev)
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(q)


# ------------------------------------------------------------------ analytics & evaluation
def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 1)


@app.get("/api/analytics")
def analytics(user: User = Depends(current_user)) -> dict:
    """Assistant usage: intents, latency per route, tool usage, validation outcomes (admins see everyone)."""
    with session() as db:
        q = db.query(Message).filter(Message.role == "assistant", Message.route != "confirm")
        if user.crm_role != "admin":
            q = q.filter(Message.user_id == user.id)
        rows = q.all()
    by_intent: dict[str, list[float]] = {}
    tools: Counter = Counter()
    denied = 0
    validated = regenerated = fallback = failed_first = 0
    for m in rows:
        by_intent.setdefault(m.intent or "unknown", []).append(m.latency_ms)
        for t in m.tool_calls:
            tools[t["tool"]] += 1
            denied += t.get("kind") == "denied"
        v = m.validation or {}
        if v.get("checked") and m.intent == "crm_data":
            validated += 1
            failed_first += bool(v.get("first_issues"))
            regenerated += bool(v.get("regenerated"))
            fallback += bool(v.get("fallback"))
    lat = [m.latency_ms for m in rows]
    return {"messages": len(rows), "p50_ms": _pct(lat, 0.5), "p95_ms": _pct(lat, 0.95),
            "intents": [{"intent": k, "count": len(v), "p50_ms": _pct(v, 0.5)} for k, v in sorted(by_intent.items())],
            "tools": [{"tool": k, "count": v} for k, v in tools.most_common()], "access_denied": denied,
            "validation": {"validated": validated, "failed_first_draft": failed_first, "regenerated": regenerated, "fallback": fallback},
            "llm_calls_avg": round(statistics.mean(m.llm_calls for m in rows), 2) if rows else 0}


@app.get("/api/eval/runs")
def eval_runs(user: User = Depends(current_user)) -> list[dict]:
    with session() as db:
        return [r.to_dict() for r in db.query(EvalRun).order_by(EvalRun.id).all()]


@app.get("/api/eval/runs/{run_id}")
def eval_run(run_id: int, user: User = Depends(current_user)) -> dict:
    with session() as db:
        r = db.get(EvalRun, run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    d = r.to_dict()
    path = PROJECT_ROOT / r.results_file if r.results_file else None
    d["details"] = json.loads(path.read_text(encoding="utf-8")).get("details", {}) if path and path.exists() else {}
    return d

"""Seed users/workspaces, the demo CRM and the knowledge documents listed in data/manifest.json."""
from __future__ import annotations

import json
import logging

from .config import PROJECT_ROOT, get_settings
from .crm.seed_data import seed_crm, seed_users
from .db import Document, session

log = logging.getLogger("crm.seed")
MANIFEST = PROJECT_ROOT / "data" / "manifest.json"


def seed_identities() -> None:
    seed_users()


def seed_all_crm(reset: bool = False) -> dict:
    return seed_crm(get_settings().crm_seed, reset=reset)


def seed_documents(ingest: bool = True) -> list[int]:
    from .ingest.pipeline import ingest_document, register_document

    if not MANIFEST.exists():
        log.warning("missing %s - run data/make_docs.py", MANIFEST)
        return []
    new_ids: list[int] = []
    for e in json.loads(MANIFEST.read_text(encoding="utf-8")):
        path = PROJECT_ROOT / e["path"]
        if not path.exists():
            log.warning("missing %s - run data/make_docs.py", path)
            continue
        with session() as db:
            existing = db.query(Document).filter(Document.path == e["path"]).first()
            if existing is not None and existing.status == "ready":
                continue
            doc_id = existing.id if existing else register_document(path, e["workspace"], e.get("title"), e.get("source", ""))
        if ingest:
            doc = ingest_document(doc_id)
            log.info("ingested %s: %s chunks, %.1fs", doc.title, doc.n_chunks, doc.ingest_seconds)
        new_ids.append(doc_id)
    return new_ids

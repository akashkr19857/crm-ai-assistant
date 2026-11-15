from __future__ import annotations

import io

from conftest import auth

from app.llm import FakeLLM, set_llm


def test_health_and_login(client):
    assert client.get("/api/health").json()["status"] == "ok"
    r = client.post("/api/login", json={"user_id": "lena"})
    assert r.status_code == 200 and r.json()["token"] == "demo-lena"
    assert client.post("/api/login", json={"user_id": "mallory"}).status_code == 404


def test_auth_required(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/me", headers=auth("pbuser")).json()["workspaces"] == ["sales-playbook"]


def test_documents_are_scoped_to_workspaces(client, seeded):
    titles = {d["title"] for d in client.get("/api/documents", headers=auth("lena")).json()}
    assert titles == {"AI Risk Test Doc"}
    assert len(client.get("/api/documents", headers=auth("admin")).json()) == 2
    assert client.get("/api/documents?workspace=sales-playbook", headers=auth("lena")).status_code == 403
    # direct access to another workspace's document is hidden as 404
    assert client.get(f"/api/documents/{seeded['sec']}/chunks", headers=auth("lena")).status_code == 404
    assert client.get(f"/api/documents/{seeded['sec']}/pages/1.png?token=demo-lena").status_code == 404


def test_page_image_with_highlight(client, seeded):
    chunks = client.get(f"/api/documents/{seeded['ai']}/chunks?page=1", headers=auth("lena")).json()
    r = client.get(f"/api/documents/{seeded['ai']}/pages/1.png", params={"token": "demo-lena", "chunk_id": chunks[0]["id"]})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert client.get(f"/api/documents/{seeded['ai']}/pages/99.png?token=demo-lena").status_code == 404


def test_upload_validation(client):
    files = {"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")}
    assert client.post("/api/documents", headers=auth("admin"), files=files, data={"workspace": "product-docs"}).status_code == 400
    files = {"file": ("fake.pdf", io.BytesIO(b"not a pdf"), "application/pdf")}
    assert client.post("/api/documents", headers=auth("admin"), files=files, data={"workspace": "product-docs"}).status_code == 400
    files = {"file": ("x.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")}
    assert client.post("/api/documents", headers=auth("lena"), files=files, data={"workspace": "sales-playbook"}).status_code == 403


def test_eval_endpoints_empty_ok(client):
    assert client.get("/api/eval/runs", headers=auth("admin")).status_code == 200
    assert client.get("/api/eval/runs/9999", headers=auth("admin")).status_code == 404

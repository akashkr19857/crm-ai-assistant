"""End-to-end test of the real running app (API :8300 + dashboard :3300 + Ollama), driven by Playwright.

Start the app first (run_demo.bat), then:  python -m pytest tests/e2e -q -p no:cacheprovider
Skipped automatically when the app is not running.
"""
from __future__ import annotations

import os
import urllib.request

import pytest

WEB = os.environ.get("CRM_WEB", "http://localhost:3300")
API = os.environ.get("CRM_API", "http://localhost:8300")


def _up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not (_up(WEB) and _up(f"{API}/api/health")), reason="app is not running on :3300/:8300")


def _ask(page, text: str) -> None:
    before = page.get_by_test_id("assistant-message").count()
    page.get_by_test_id("chat-input").fill(text)
    page.get_by_test_id("send").click()
    page.wait_for_function(f"() => document.querySelectorAll('[data-testid=assistant-message]').length > {before}", timeout=240_000)


def _login(page, user: str) -> None:
    page.goto(WEB)
    page.evaluate("localStorage.clear()")
    page.reload()
    page.get_by_test_id(f"login-{user}").click()
    page.get_by_test_id("chat-input").wait_for()


def test_assistant_flow():
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        _login(page, "ananya")
        _ask(page, "What is my open pipeline by stage?")
        last = page.get_by_test_id("assistant-message").last
        expect(last.get_by_test_id("intent-badge")).to_have_text("CRM data")
        expect(last.get_by_test_id("validation-badge")).to_be_visible()
        expect(page.get_by_test_id("tool-call").first).to_contain_text("pipeline_summary")

        _ask(page, "Tell me about Cedar Energy")
        _ask(page, "What about their open tickets?")
        expect(page.get_by_test_id("standalone-query")).to_contain_text("Cedar Energy")

        _ask(page, "Create a task on Cedar Energy to call the CFO tomorrow")
        page.get_by_test_id("action-card").last.wait_for()
        page.get_by_test_id("confirm-action").last.click()
        expect(page.get_by_test_id("assistant-message").last).to_contain_text("Done", timeout=30_000)

        # RBAC: support agent cannot see pipeline, in chat or in the CRM explorer
        _login(page, "lena")
        _ask(page, "Show me the pipeline")
        expect(page.get_by_test_id("assistant-message").last).to_contain_text("can't show")
        page.goto(f"{WEB}/crm")
        page.get_by_test_id("crm-tab-pipeline").click()
        expect(page.get_by_test_id("crm-denied")).to_contain_text("cannot access")
        browser.close()

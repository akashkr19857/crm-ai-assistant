"""Record the demo video of the real running app (start it with run_demo.bat first).

    python demo/record_demo.py        → demo/demo.mp4, demo/demo.gif, demo/screenshots/*.png

Uses the shared recorder (Playwright video + ffmpeg) in ../_shared/record_demo.py. Every answer in the video is
generated live by the local LLM against the demo CRM.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "_shared"))
from record_demo import record  # noqa: E402

WEB = "http://localhost:3300"
SHOTS = HERE / "screenshots"


async def shot(page, name: str) -> None:
    await page.screenshot(path=str(SHOTS / f"{name}.png"))


async def ask(page, text: str, timeout: int = 240_000) -> None:
    before = await page.locator("[data-testid=assistant-message]").count()
    await page.fill("[data-testid=chat-input]", text)
    await page.click("[data-testid=send]")
    await page.wait_for_function(f"() => document.querySelectorAll('[data-testid=assistant-message]').length > {before}", timeout=timeout)
    await page.wait_for_timeout(600)


async def login(page, user: str) -> None:
    await page.goto(WEB)
    await page.evaluate("localStorage.clear()")
    await page.goto(WEB)
    await page.wait_for_selector(f"[data-testid=login-{user}]")
    await page.click(f"[data-testid=login-{user}]")
    await page.wait_for_selector("[data-testid=chat-input]")


async def steps(page, caption) -> None:
    SHOTS.mkdir(exist_ok=True)
    await page.goto(WEB)
    await page.evaluate("localStorage.clear()")
    await page.goto(WEB)
    await page.wait_for_selector("[data-testid=login-ananya]")
    await caption("Each role sees different CRM data; the assistant enforces the same rules", 3500)
    await login(page, "ananya")
    await caption("Signed in as Ananya, a sales rep. Ask about the CRM in plain English", 2500)

    await ask(page, "What is my open pipeline by stage?")
    await caption("Intent: CRM data → pipeline_summary tool → answer → every number validated", 5000)
    await shot(page, "01_pipeline")

    await ask(page, "Tell me about Cedar Energy")
    await caption("Account 360 from the CRM API, with contacts, deals and tickets", 3500)
    await ask(page, "What about their open tickets?")
    await caption("Multi-turn: 'their' is resolved to Cedar Energy from conversation memory", 5000)
    await shot(page, "02_multi_turn")

    await ask(page, "Who has to approve a 15% discount?")
    await caption("Knowledge question → RAG over the sales playbook, with a citation", 3000)
    cite = page.locator("[data-testid=assistant-message]").last.locator("[data-testid=citation]")
    if await cite.count():
        await cite.first.click()
        await page.wait_for_timeout(1500)
    await caption("Click a citation to see the exact highlighted source page", 4000)
    await shot(page, "03_knowledge")

    await ask(page, "Create a task on Cedar Energy to send the renewal quote next week")
    await page.wait_for_selector("[data-testid=action-card]")
    await caption("Workflow intent: the assistant prepares the change but waits for confirmation", 4000)
    await page.click("[data-testid=confirm-action]")
    await page.wait_for_timeout(2000)
    await caption("Confirmed → task created through the CRM API and written to the audit log", 3500)
    await shot(page, "04_workflow")

    await page.goto(f"{WEB}/crm")
    await page.click("[data-testid=crm-tab-audit]")
    await page.wait_for_selector("[data-testid=audit-log]")
    await caption("Audit log of every change made through the assistant", 3000)
    await page.click("[data-testid=crm-tab-accounts]")
    await page.wait_for_selector("[data-testid=accounts-table]")
    await caption("CRM data page: Ananya sees only her own 10 accounts", 3000)

    await login(page, "lena")
    await caption("Now Lena, a support agent: no access to deals or revenue", 2500)
    await ask(page, "Show me the pipeline")
    await caption("Access control: the tool call is denied for her role, nothing leaks", 4000)
    await ask(page, "How many open P1 or P2 tickets are there?")
    await caption("But support data works, from the same assistant", 3500)
    await shot(page, "05_support_rbac")
    await ask(page, "How do I fix an SSO login loop after a password reset?")
    await caption("Product help answered from the help center, with citation", 3500)

    await login(page, "admin")
    await page.goto(f"{WEB}/analytics")
    await page.wait_for_timeout(2500)
    await caption("Analytics: intents, tool usage, latency, validation outcomes", 3500)
    await shot(page, "06_analytics")
    await page.goto(f"{WEB}/evaluation")
    await page.wait_for_selector("[data-testid=eval-metric]")
    await caption("Measured accuracy on scripted conversations vs SQL ground truth", 5000)
    await shot(page, "07_evaluation")
    await caption("")


if __name__ == "__main__":
    record(steps, out_dir=HERE, title="CRM AI Assistant", subtitle="GenAI · RAG · intent routing · CRM tools · local LLM")

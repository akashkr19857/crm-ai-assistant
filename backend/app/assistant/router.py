"""Intent detection + query routing with multi-turn context.

One JSON-mode LLM call turns the latest user message (plus recent history and the conversation's entity memory) into:
* intent: knowledge | crm_data | workflow | chitchat | out_of_scope
* standalone_query: the message rewritten to be self-contained (pronouns like "their" resolved from memory)
* account: the account in focus, if any
A keyword classifier is the fallback when the model's output is unusable, so routing never fails.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..llm import get_llm, parse_json

INTENTS = ("knowledge", "crm_data", "workflow", "chitchat", "out_of_scope")

ROUTER_SYSTEM = """You are the router of an AI assistant inside NimbusCRM, a CRM product used by sales and support teams.
Classify the user's LATEST message into exactly one intent:
- "crm_data": questions about the company's CRM records: accounts, contacts, deals, pipeline, revenue/ARR, renewals,
  health scores, activities/tasks, support tickets ("show my open deals", "which accounts renew this quarter",
  "how many P1 tickets are open", "what about their contacts?").
- "knowledge": how-to / product / policy questions answered from documentation: product features and troubleshooting,
  SLAs, pricing, discount approval rules, sales process, playbooks ("how do I fix SSO login loop", "who approves a 15% discount").
- "workflow": the user asks to DO something that changes CRM data: create a task/reminder/follow-up, log a note,
  move/update a deal stage, escalate a ticket.
- "chitchat": greetings, thanks, questions about what the assistant can do.
- "out_of_scope": anything unrelated to the CRM, the product or the user's sales/support work.
Then rewrite the latest message as a standalone query that can be understood without the conversation: replace
pronouns and references ("their", "that deal", "it", "the second one") with the concrete names from the history or
the memory. Keep the user's wording otherwise. Also return the account name in focus (null if none).
Reply as JSON: {"intent": "...", "standalone_query": "...", "account": "<name or null>"}"""

_WORKFLOW = re.compile(r"\b(create|add|set|schedule|remind|log|record|move|update|change|mark|escalate|close)\b.*\b(task|reminder|"
                       r"follow[- ]?up|note|stage|deal|ticket|won|lost)\b", re.I)
_CRM = re.compile(r"\b(my|our|open|show|list|how many|which|top|largest|account|accounts|deal|deals|pipeline|arr|revenue|"
                  r"renewal|renew|ticket|tickets|task|tasks|contact|contacts|health|forecast|activities)\b", re.I)
_KNOWLEDGE = re.compile(r"\b(how (do|can|to)|what is the|policy|sla|discount|price|pricing|edition|approve|troubleshoot|fix|"
                        r"error|sync|sso|webhook|api|import|stage exit|playbook|limit)\b", re.I)
_CHITCHAT = re.compile(r"^\s*(hi|hello|hey|thanks|thank you|good (morning|afternoon|evening)|what can you do|help)\b", re.I)


@dataclass
class Route:
    intent: str
    standalone_query: str
    account: str | None = None
    source: str = "llm"  # llm | fallback
    raw: dict = field(default_factory=dict)


def keyword_intent(text: str) -> str:
    if _CHITCHAT.search(text):
        return "chitchat"
    if _WORKFLOW.search(text):
        return "workflow"
    if _KNOWLEDGE.search(text) and not re.search(r"\b(my|our)\b", text, re.I):
        return "knowledge"
    if _CRM.search(text):
        return "crm_data"
    if _KNOWLEDGE.search(text):
        return "knowledge"
    return "out_of_scope"


def _history_text(history: list[dict], limit: int) -> str:
    lines = []
    for m in history[-limit:]:
        content = str(m.get("content", ""))[:500]
        lines.append(f"{m.get('role', 'user')}: {content}")
    return "\n".join(lines) or "(no previous messages)"


def route(message: str, history: list[dict], memory: dict, limit: int = 6, counter=None) -> Route:  # noqa: ANN001
    mem = ", ".join(f"{k}: {v}" for k, v in memory.items() if v) or "(empty)"
    user = f"Conversation so far:\n{_history_text(history, limit)}\n\nMemory (entities in focus): {mem}\n\nLatest user message: {message}"
    res = get_llm().chat([{"role": "system", "content": ROUTER_SYSTEM}, {"role": "user", "content": user}],
                         json_mode=True, temperature=0.0, max_tokens=200)
    if counter is not None:
        counter.add(res.prompt_tokens, res.completion_tokens)
    data = parse_json(res.text)
    intent = str(data.get("intent", "")).strip().lower()
    if intent not in INTENTS:
        return Route(keyword_intent(message), message, memory.get("account"), "fallback", data)
    query = str(data.get("standalone_query") or message).strip() or message
    account = data.get("account")
    account = str(account).strip() if account and str(account).lower() not in ("null", "none", "") else None
    return Route(intent, query, account, "llm", data)

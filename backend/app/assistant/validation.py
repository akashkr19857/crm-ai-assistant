"""Response validation for CRM-data answers.

Every number (amounts, counts, percentages, health scores, ids) and every date in the answer must be supported by the
tool results the answer was generated from (or by the user's own question). Money written as "$1.2M" or "$982K" is
matched with a tolerance, so rounding is allowed but invented figures are not.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_NUM = re.compile(r"(?<![\w.])\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s?(k|K|m|M|million|thousand|bn|billion)?(?![\w])")
_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
_LONG_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December|"
                        r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b")
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}
_PRIORITY_OR_CODE = re.compile(r"\b[Pp][1-4]\b|\bQ[1-4]\b")


def collect_values(obj: Any, nums: set[float], dates: set[str]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        nums.add(float(obj))
    elif isinstance(obj, str):
        for m in _ISO.finditer(obj):
            dates.add(m.group(0))
        if re.fullmatch(r"-?\d+(\.\d+)?", obj.strip()):
            nums.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            collect_values(v, nums, dates)
    elif isinstance(obj, list):
        nums.add(float(len(obj)))
        for v in obj:
            collect_values(v, nums, dates)


def numbers_in(text: str) -> list[tuple[str, float, bool]]:
    """(raw text, value, is_abbreviated) for every number in the text, ignoring dates and codes like P1/Q4."""
    cleaned = _ISO.sub(" ", _LONG_DATE.sub(" ", _PRIORITY_OR_CODE.sub(" ", text)))
    out = []
    for m in _NUM.finditer(cleaned):
        whole, frac, unit = m.group(1).replace(",", ""), m.group(2), (m.group(3) or "").lower()
        value = float(f"{whole}.{frac}" if frac else whole) * _MULT.get(unit, 1.0)
        out.append((m.group(0).strip(), value, bool(unit)))
    return out


def dates_in(text: str) -> list[tuple[str, str]]:
    out = [(m.group(0), m.group(0)) for m in _ISO.finditer(text)]
    for m in _LONG_DATE.finditer(text):
        month = m.group(1)[:3]
        try:
            iso = datetime.strptime(f"{month} {m.group(2)} {m.group(3)}", "%b %d %Y").date().isoformat()
        except ValueError:
            continue
        out.append((m.group(0), iso))
    return out


def _supported(value: float, abbreviated: bool, allowed: set[float]) -> bool:
    for a in allowed:
        if abs(a - value) <= 0.5:
            return True
        if abbreviated and a and abs(a - value) / abs(a) <= 0.06:  # "$1.2M" for 1,234,000
            return True
        if abs(a * 100 - value) <= 0.5:  # ratio shown as a percentage
            return True
    return False


def validate_answer(answer: str, results: list[Any], question: str = "") -> dict:
    nums: set[float] = set()
    dates: set[str] = set()
    for r in results:
        collect_values(r, nums, dates)
    for _, v, _ in numbers_in(question):
        nums.add(v)
    for _, iso in dates_in(question):
        dates.add(iso)
    issues = []
    for raw, value, abbr in numbers_in(answer):
        if value <= 1 and not abbr:  # "1 deal", "one of" - too weak a signal to judge
            continue
        if not _supported(value, abbr, nums):
            issues.append(f"number {raw} is not in the CRM data")
    for raw, iso in dates_in(answer):
        if iso not in dates:
            issues.append(f"date {raw} is not in the CRM data")
    return {"checked": True, "passed": not issues, "issues": issues}


def fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def render_fallback(results: list[dict]) -> str:
    """Deterministic answer straight from tool results, used when the LLM answer fails validation twice."""
    parts: list[str] = []
    for call in results:
        r = call.get("result", {})
        name = call.get("tool", "")
        if name == "get_account":
            a = r.get("account", {})
            line = f"**{a.get('name')}** ({a.get('industry')}, {a.get('tier')}, {a.get('region')}) - owner {a.get('owner')}, health {a.get('health_score')}"
            if "arr" in a:
                line += f", ARR {fmt_money(a['arr'])}"
            parts.append(line + f", renewal {a.get('renewal_date')}.")
            if r.get("open_deals") is not None:
                parts.append(f"Open deals: {len(r['open_deals'])} totalling {fmt_money(r.get('open_pipeline', 0))}.")
            parts.append(f"Open tickets: {len(r.get('open_tickets', []))}.")
            continue
        if "groups" in r:
            parts.append(f"Open pipeline: {r['count']} deals, {fmt_money(r['total_amount'])} total, {fmt_money(r['weighted_amount'])} weighted.")
            parts += [f"- {g['group']}: {g['count']} deals, {fmt_money(g['total_amount'])}" for g in r["groups"]]
            continue
        for key, label in (("deals", "deals"), ("tickets", "tickets"), ("accounts", "accounts"), ("activities", "activities")):
            if key in r:
                head = f"{r.get('count', len(r[key]))} {label}"
                if "total_amount" in r:
                    head += f", {fmt_money(r['total_amount'])} total"
                parts.append(head + ":")
                for row in r[key][:10]:
                    desc = row.get("name") or row.get("subject")
                    extra = row.get("stage") or row.get("status") or row.get("tier") or row.get("kind") or ""
                    amount = f" {fmt_money(row['amount'])}" if "amount" in row else ""
                    acct = f" ({row['account']})" if row.get("account") and row.get("account") != desc else ""
                    parts.append(f"- {desc}{acct} {extra}{amount}".rstrip())
    return "\n".join(parts) or "No matching records."
